# oauth_router.py
import base64
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from firm.core.interfaces import Tenant
from firm.oauth2.models import (
    AuthorizationCode,
    OAuth2Client,
    OAuth2Token,
    RefreshToken,
)
from firm.oauth2.store import FirmOAuth2DataStore

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def get_issuer(request: Request) -> str:
    scheme = request.url.scheme
    host = request.headers.get("host", "localhost:8000")
    return f"{scheme}://{host}"


async def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Stub: replace with real user lookup."""
    if username and password:
        return {"user_id": username}
    return None


# -----------------------------------------------------------------------------
# Router factory
# -----------------------------------------------------------------------------


def create_oauth2_router() -> APIRouter:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.oauth2_stores = {}
        yield

    router = APIRouter(lifespan=lifespan)

    # http_bearer = HTTPBearer(auto_error=False)

    # --- Shared dependency ------------------------------------------------

    # async def get_current_token(
    #     credentials: HTTPAuthorizationCredentials = Depends(http_bearer),
    # ) -> OAuth2Token:
    #     if not credentials or credentials.scheme.lower() != "bearer":
    #         raise HTTPException(
    #             status_code=status.HTTP_401_UNAUTHORIZED,
    #             detail="Missing or invalid authorization header",
    #         )
    #     token = await _store.get_token(credentials.credentials)
    #     if not token:
    #         raise HTTPException(
    #             status_code=status.HTTP_401_UNAUTHORIZED,
    #             detail="Invalid or expired token",
    #         )
    #     return token

    # # Expose for external use (e.g. main.py)
    # router.get_current_token = get_current_token  # type: ignore[attr-defined]

    # --- Endpoints --------------------------------------------------------

    @router.get("/oauth/authorize", response_class=HTMLResponse)
    async def authorize_get(request: Request):
        """
        Authorization endpoint (RFC 6749 §3.1) — display login/consent form.
        """
        params = dict(request.query_params)
        response_type = params.get("response_type")
        client_id = params.get("client_id")
        redirect_uri = params.get("redirect_uri", "")
        scope = params.get("scope", "")
        state = params.get("state", "")

        if response_type != "code":
            raise HTTPException(status_code=400, detail="Only response_type=code is supported")
        if not client_id:
            raise HTTPException(status_code=400, detail="client_id is required")

        store = get_store(request)
        client = await store.get_client(client_id)
        if not client:
            raise HTTPException(status_code=400, detail="Unknown client_id")

        # Validate redirect_uri
        if redirect_uri and redirect_uri not in client.redirect_uris:
            raise HTTPException(status_code=400, detail="Invalid redirect_uri")
        effective_redirect = redirect_uri or (
            client.redirect_uris[0] if client.redirect_uris else ""
        )

        hidden = ""
        for k, v in [
            ("client_id", client_id),
            ("redirect_uri", effective_redirect),
            ("scope", scope),
            ("state", state),
            ("response_type", response_type),
        ]:
            hidden += f'<input type="hidden" name="{k}" value="{v}">\n'

        html = f"""
        <!DOCTYPE html>
        <html>
        <head><title>Authorize</title></head>
        <body>
          <h2>Sign in to authorize <strong>{client_id}</strong></h2>
          <p>Requested scope: <code>{scope or '(none)'}</code></p>
          <form method="post" action="/oauth/authorize">
            {hidden}
            <label>Username: <input type="text" name="username" autofocus required></label><br><br>
            <label>Password: <input type="password" name="password" required></label><br><br>
            <button type="submit">Authorize</button>
          </form>
        </body>
        </html>
        """
        return HTMLResponse(content=html)

    @router.post("/oauth/authorize")
    async def authorize_post(request: Request):
        """
        Authorization endpoint — validate credentials and redirect with code.
        """
        form = await request.form()
        body = dict(form)

        client_id = body.get("client_id", "")
        redirect_uri = body.get("redirect_uri", "")
        scope = body.get("scope", "")
        state = body.get("state", "")
        username = body.get("username", "")
        password = body.get("password", "")

        if not client_id:
            raise HTTPException(status_code=400, detail="client_id is required")

        store = get_store(request)
        client = store.get_client(client_id)
        if not client:
            raise HTTPException(status_code=400, detail="Unknown client_id")

        user = await authenticate_user(username, password)
        if not user:
            # Re-render form with error
            hidden = ""
            for k, v in [
                ("client_id", client_id),
                ("redirect_uri", redirect_uri),
                ("scope", scope),
                ("state", state),
                ("response_type", "code"),
            ]:
                hidden += f'<input type="hidden" name="{k}" value="{v}">\n'
            html = f"""
            <!DOCTYPE html>
            <html>
            <head><title>Authorize</title></head>
            <body>
              <h2>Sign in to authorize <strong>{client_id}</strong></h2>
              <p style="color:red">Invalid username or password.</p>
              <p>Requested scope: <code>{scope or '(none)'}</code></p>
              <form method="post" action="/oauth/authorize">
                {hidden}
                <label>Username: <input type="text" name="username" autofocus required></label><br><br>
                <label>Password: <input type="password" name="password" required></label><br><br>
                <button type="submit">Authorize</button>
              </form>
            </body>
            </html>
            """
            return HTMLResponse(content=html, status_code=401)

        code = secrets.token_urlsafe(24)
        auth_code = AuthorizationCode(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            user_id=user["user_id"],
            issued_at=int(time.time()),
        )

        store = get_store(request)
        await store.save_code(auth_code)

        qs = urlencode({"code": code, **(({"state": state}) if state else {})})
        return RedirectResponse(url=f"{redirect_uri}?{qs}", status_code=302)

    @router.post("/oauth/token")
    async def issue_token(request: Request):
        """
        Supports authorization_code, password and client_credentials grants (RFC 6749).

        Form body (application/x-www-form-urlencoded or JSON):
          grant_type=authorization_code&code=...&redirect_uri=...&client_id=...&client_secret=...
          grant_type=password&username=...&password=...&client_id=...&client_secret=...
        Client credentials may also be supplied via HTTP Basic auth.
        """
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body: Dict[str, Any] = await request.json()
        else:
            form = await request.form()
            body = dict(form)

        grant_type = body.get("grant_type")
        if not grant_type:
            raise HTTPException(status_code=400, detail="grant_type is required")

        # Resolve client credentials from body or HTTP Basic auth
        client_id: str = body.get("client_id", "")
        client_secret: str = body.get("client_secret", "")

        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                client_id, client_secret = decoded.split(":", 1)
            except Exception:
                raise HTTPException(status_code=401, detail="Invalid Basic auth header")

        if not client_id:
            raise HTTPException(status_code=401, detail="client_id is required")

        store = get_store(request)
        client = await store.authenticate_client(client_id, client_secret)
        if not client:
            raise HTTPException(status_code=401, detail="Invalid client credentials")

        if grant_type not in client.grant_types:
            raise HTTPException(
                status_code=400,
                detail=f"Client does not support grant_type: {grant_type}",
            )

        if grant_type == "authorization_code":
            code = body.get("code")
            if not code:
                raise HTTPException(status_code=400, detail="code is required")
            auth_code = await store.consume_code(code)
            if not auth_code:
                raise HTTPException(status_code=400, detail="Invalid or expired authorization code")
            if auth_code.client_id != client_id:
                raise HTTPException(status_code=400, detail="code was not issued to this client")
            req_redirect = body.get("redirect_uri", "")
            if req_redirect and req_redirect != auth_code.redirect_uri:
                raise HTTPException(status_code=400, detail="redirect_uri mismatch")
            user_id = auth_code.user_id
            scope = auth_code.scope
        elif grant_type == "password":
            username = body.get("username")
            password = body.get("password")
            if not username or not password:
                raise HTTPException(status_code=400, detail="username and password are required")
            user = await authenticate_user(username, password)
            if not user:
                raise HTTPException(status_code=401, detail="Invalid user credentials")
            user_id = user["user_id"]
            scope = body.get("scope", client.scope)
        elif grant_type == "client_credentials":
            user_id = client_id
            scope = body.get("scope", client.scope)
        elif grant_type == "refresh_token":
            raw_refresh = body.get("refresh_token")
            if not raw_refresh:
                raise HTTPException(status_code=400, detail="refresh_token is required")
            stored_rt = await store.get_refresh_token(raw_refresh)
            if not stored_rt:
                raise HTTPException(status_code=400, detail="Invalid or expired refresh_token")
            if stored_rt.client_id != client_id:
                raise HTTPException(
                    status_code=400, detail="refresh_token was not issued to this client"
                )
            # Rotate: invalidate the old refresh token
            await store.delete_refresh_token(raw_refresh)
            user_id = stored_rt.user_id
            scope = body.get("scope", stored_rt.scope)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported grant_type: {grant_type}",
            )

        access_token = secrets.token_urlsafe(32)
        expires_in = 3600
        token = OAuth2Token(
            access_token=access_token,
            client_id=client_id,
            user_id=user_id,
            scope=scope,
            issued_at=int(time.time()),
            expires_in=expires_in,
        )

        await store.save_token(token)

        actor = await get_actor(request, user_id)

        response_body: Dict[str, Any] = {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": expires_in,
            "scope": token.scope,
            "me": actor["id"],  # ActivityPub actor URL
        }

        # Issue a refresh token for grants that bind to a user identity
        if grant_type in ("authorization_code", "password", "refresh_token"):
            rt_value = secrets.token_urlsafe(40)
            refresh_token = RefreshToken(
                refresh_token=rt_value,
                client_id=client_id,
                user_id=user_id,
                scope=scope,
                issued_at=int(time.time()),
            )
            await store.save_refresh_token(refresh_token)
            response_body["refresh_token"] = rt_value

        return JSONResponse(content=response_body)

    @router.post("/oauth/register")
    async def register_client(request: Request):
        """
        Dynamic Client Registration (RFC 7591).
        Open registration - add an auth check here if needed.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Request body must be valid JSON")

        client_id = secrets.token_urlsafe(16)
        client_secret = secrets.token_urlsafe(32)

        client = OAuth2Client(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=body.get("redirect_uris", []),
            grant_types=body.get("grant_types", ["authorization_code", "refresh_token"]),
            response_types=body.get("response_types", ["code"]),
            scope=body.get("scope", ""),
            token_endpoint_auth_method=body.get(
                "token_endpoint_auth_method", "client_secret_basic"
            ),
        )

        store = get_store(request)
        await store.save_client(client)

        return JSONResponse(
            status_code=201,
            content={
                "client_id": client.client_id,
                "client_secret": client.client_secret,
                "redirect_uris": client.redirect_uris,
                "grant_types": client.grant_types,
                "response_types": client.response_types,
                "scope": client.scope,
                "token_endpoint_auth_method": client.token_endpoint_auth_method,
            },
        )

    def get_store(request):
        tenant = request.state.tenant
        stores = request.app.state.oauth2_stores
        store = stores.get(tenant.prefix)
        if not store:
            store = FirmOAuth2DataStore(tenant)
            stores[tenant.prefix] = store
        return store

    async def get_actor(request, user_id: str):
        tenant: Tenant = request.state.tenant
        return await tenant.public_store.query_one({"preferredUsername": user_id})

    @router.post("/oauth/revoke")
    async def revoke_token(request: Request):
        """
        Token revocation endpoint (RFC 7009).

        Form body:
          token=<value>&token_type_hint=access_token|refresh_token
        Client authentication via HTTP Basic auth or body params is required.
        Always returns 200 (per spec) even if the token is unknown.
        """
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body: Dict[str, Any] = await request.json()
        else:
            form = await request.form()
            body = dict(form)

        # Client authentication
        rev_client_id: str = body.get("client_id", "")
        rev_client_secret: str = body.get("client_secret", "")
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                rev_client_id, rev_client_secret = decoded.split(":", 1)
            except Exception:
                raise HTTPException(status_code=401, detail="Invalid Basic auth header")

        if not rev_client_id:
            raise HTTPException(status_code=401, detail="client_id is required")

        store = get_store(request)
        rev_client = await store.authenticate_client(rev_client_id, rev_client_secret)
        if not rev_client:
            raise HTTPException(status_code=401, detail="Invalid client credentials")

        token_value: str = body.get("token", "")
        token_type_hint: str = body.get("token_type_hint", "")

        if not token_value:
            # Per RFC 7009 §2.1 — missing token is not an error
            return JSONResponse(content={})

        # Try to revoke as access token and/or refresh token
        if token_type_hint == "refresh_token":
            await store.delete_refresh_token(token_value)
        elif token_type_hint == "access_token":
            await store.delete_token(token_value)
        else:
            # No hint — try both
            await store.delete_token(token_value)
            await store.delete_refresh_token(token_value)

        return JSONResponse(content={})

    @router.get("/.well-known/oauth-authorization-server", response_class=JSONResponse)
    async def oauth_authorization_server(request: Request):
        issuer = get_issuer(request)
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "registration_endpoint": f"{issuer}/oauth/register",
            "revocation_endpoint": f"{issuer}/oauth/revoke",
            "response_types_supported": ["code", "token"],
            "grant_types_supported": [
                "authorization_code",
                "password",
                "client_credentials",
                "refresh_token",
            ],
            "scopes_supported": ["openid", "profile", "email", "read", "write"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "revocation_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
            ],
        }

    return router
