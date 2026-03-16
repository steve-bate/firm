import time
from typing import Dict, Optional, cast

import aiosqlite

from firm.core.interfaces import JSONObject, Tenant
from firm.oauth2.models import (
    AuthorizationCode,
    OAuth2Client,
    OAuth2Token,
    RefreshToken,
)

# -----------------------------------------------------------------------------
# Storage interface
# -----------------------------------------------------------------------------


class OAuth2DataStore:
    """Single storage interface for all OAuth2 objects.
    Subclass this (or replace with a DB-backed implementation) as needed.
    """

    # Clients
    async def get_client(self, client_id: str) -> Optional[OAuth2Client]:
        raise NotImplementedError

    async def save_client(self, client: OAuth2Client) -> None:
        raise NotImplementedError

    async def authenticate_client(
        self, client_id: str, client_secret: str
    ) -> Optional[OAuth2Client]:
        raise NotImplementedError

    # Access tokens
    async def save_token(self, token: OAuth2Token) -> None:
        raise NotImplementedError

    async def get_token(self, access_token: str) -> Optional[OAuth2Token]:
        raise NotImplementedError

    async def delete_token(self, access_token: str) -> None:
        raise NotImplementedError

    # Refresh tokens
    async def save_refresh_token(self, token: RefreshToken) -> None:
        raise NotImplementedError

    async def get_refresh_token(self, refresh_token: str) -> Optional[RefreshToken]:
        raise NotImplementedError

    async def delete_refresh_token(self, refresh_token: str) -> None:
        raise NotImplementedError

    # Authorization codes
    async def save_code(self, auth_code: AuthorizationCode) -> None:
        raise NotImplementedError

    async def consume_code(self, code: str) -> Optional[AuthorizationCode]:
        """Return and delete the code (single-use)."""
        raise NotImplementedError


# -----------------------------------------------------------------------------
# In-memory implementation (swap for file/sqlite/etc.)
# -----------------------------------------------------------------------------


class InMemoryOAuth2DataStore(OAuth2DataStore):
    def __init__(self) -> None:
        self._clients: Dict[str, OAuth2Client] = {}
        self._tokens: Dict[str, OAuth2Token] = {}
        self._refresh_tokens: Dict[str, RefreshToken] = {}
        self._codes: Dict[str, AuthorizationCode] = {}

    # Clients
    async def get_client(self, client_id: str) -> Optional[OAuth2Client]:
        return self._clients.get(client_id)

    async def save_client(self, client: OAuth2Client) -> None:
        self._clients[client.client_id] = client

    async def authenticate_client(
        self, client_id: str, client_secret: str
    ) -> Optional[OAuth2Client]:
        client = self._clients.get(client_id)
        if client and client.client_secret == client_secret:
            return client
        return None

    # Access tokens
    async def save_token(self, token: OAuth2Token) -> None:
        self._tokens[token.access_token] = token

    async def get_token(self, access_token: str) -> Optional[OAuth2Token]:
        token = self._tokens.get(access_token)
        if not token:
            return None
        if int(time.time()) > token.issued_at + token.expires_in:
            return None
        return token

    async def delete_token(self, access_token: str) -> None:
        self._tokens.pop(access_token, None)

    # Refresh tokens
    async def save_refresh_token(self, token: RefreshToken) -> None:
        self._refresh_tokens[token.refresh_token] = token

    async def get_refresh_token(self, refresh_token: str) -> Optional[RefreshToken]:
        token = self._refresh_tokens.get(refresh_token)
        if not token:
            return None
        if int(time.time()) > token.issued_at + token.expires_in:
            self._refresh_tokens.pop(refresh_token, None)
            return None
        return token

    async def delete_refresh_token(self, refresh_token: str) -> None:
        self._refresh_tokens.pop(refresh_token, None)

    # Authorization codes
    async def save_code(self, auth_code: AuthorizationCode) -> None:
        self._codes[auth_code.code] = auth_code

    async def consume_code(self, code: str) -> Optional[AuthorizationCode]:
        """Return and delete the code (single-use)."""
        auth_code = self._codes.pop(code, None)
        if not auth_code:
            return None
        if int(time.time()) > auth_code.issued_at + auth_code.expires_in:
            return None
        return auth_code


# -----------------------------------------------------------------------------
# Sqlite store implementation
# -----------------------------------------------------------------------------


class SqliteOAuth2DataStore(OAuth2DataStore):
    """
    SQLite-backed implementation of OAuth2DataStore.

    Usage::

        store = SqliteOAuth2DataStore("oauth2.db")
        await store.initialize()   # create tables (idempotent)

    Pass *":memory:"* for an in-process ephemeral database.
    """

    def __init__(self, db_path: str = "oauth2.db") -> None:
        self._db_path = db_path
        self._initialized = False

    async def initialize(self) -> None:
        """Create all tables if they do not already exist."""
        if self._initialized:
            return
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS clients (
                    client_id                   TEXT PRIMARY KEY,
                    client_secret               TEXT NOT NULL,
                    redirect_uris               TEXT NOT NULL DEFAULT '',
                    grant_types                 TEXT NOT NULL DEFAULT '',
                    response_types              TEXT NOT NULL DEFAULT '',
                    scope                       TEXT NOT NULL DEFAULT '',
                    token_endpoint_auth_method  TEXT NOT NULL DEFAULT 'client_secret_basic'
                );

                CREATE TABLE IF NOT EXISTS access_tokens (
                    access_token  TEXT PRIMARY KEY,
                    client_id     TEXT NOT NULL,
                    user_id       TEXT NOT NULL,
                    scope         TEXT NOT NULL DEFAULT '',
                    issued_at     INTEGER NOT NULL,
                    expires_in    INTEGER NOT NULL,
                    token_type    TEXT NOT NULL DEFAULT 'bearer'
                );

                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    refresh_token  TEXT PRIMARY KEY,
                    client_id      TEXT NOT NULL,
                    user_id        TEXT NOT NULL,
                    scope          TEXT NOT NULL DEFAULT '',
                    issued_at      INTEGER NOT NULL,
                    expires_in     INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS authorization_codes (
                    code          TEXT PRIMARY KEY,
                    client_id     TEXT NOT NULL,
                    redirect_uri  TEXT NOT NULL DEFAULT '',
                    scope         TEXT NOT NULL DEFAULT '',
                    user_id       TEXT NOT NULL,
                    issued_at     INTEGER NOT NULL,
                    expires_in    INTEGER NOT NULL
                );
                """
            )
            await db.commit()
        self._initialized = True

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    async def get_client(self, client_id: str) -> Optional[OAuth2Client]:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return OAuth2Client(
            client_id=row["client_id"],
            client_secret=row["client_secret"],
            redirect_uris=row["redirect_uris"].split() if row["redirect_uris"] else [],
            grant_types=row["grant_types"].split() if row["grant_types"] else [],
            response_types=row["response_types"].split() if row["response_types"] else [],
            scope=row["scope"],
            token_endpoint_auth_method=row["token_endpoint_auth_method"],
        )

    async def save_client(self, client: OAuth2Client) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO clients
                    (client_id, client_secret, redirect_uris, grant_types,
                     response_types, scope, token_endpoint_auth_method)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    client_secret              = excluded.client_secret,
                    redirect_uris              = excluded.redirect_uris,
                    grant_types                = excluded.grant_types,
                    response_types             = excluded.response_types,
                    scope                      = excluded.scope,
                    token_endpoint_auth_method = excluded.token_endpoint_auth_method
                """,
                (
                    client.client_id,
                    client.client_secret,
                    " ".join(client.redirect_uris),
                    " ".join(client.grant_types),
                    " ".join(client.response_types),
                    client.scope,
                    client.token_endpoint_auth_method,
                ),
            )
            await db.commit()

    async def authenticate_client(
        self, client_id: str, client_secret: str
    ) -> Optional[OAuth2Client]:
        await self.initialize()
        client = await self.get_client(client_id)
        if client and client.client_secret == client_secret:
            return client
        return None

    # ------------------------------------------------------------------
    # Access tokens
    # ------------------------------------------------------------------

    async def save_token(self, token: OAuth2Token) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO access_tokens
                    (access_token, client_id, user_id, scope, issued_at, expires_in, token_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(access_token) DO NOTHING
                """,
                (
                    token.access_token,
                    token.client_id,
                    token.user_id,
                    token.scope,
                    token.issued_at,
                    token.expires_in,
                    token.token_type,
                ),
            )
            await db.commit()

    async def get_token(self, access_token: str) -> Optional[OAuth2Token]:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM access_tokens WHERE access_token = ?", (access_token,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        if int(time.time()) > row["issued_at"] + row["expires_in"]:
            return None
        return OAuth2Token(
            access_token=row["access_token"],
            client_id=row["client_id"],
            user_id=row["user_id"],
            scope=row["scope"],
            issued_at=row["issued_at"],
            expires_in=row["expires_in"],
            token_type=row["token_type"],
        )

    async def delete_token(self, access_token: str) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM access_tokens WHERE access_token = ?", (access_token,))
            await db.commit()

    # ------------------------------------------------------------------
    # Refresh tokens
    # ------------------------------------------------------------------

    async def save_refresh_token(self, token: RefreshToken) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO refresh_tokens
                    (refresh_token, client_id, user_id, scope, issued_at, expires_in)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(refresh_token) DO NOTHING
                """,
                (
                    token.refresh_token,
                    token.client_id,
                    token.user_id,
                    token.scope,
                    token.issued_at,
                    token.expires_in,
                ),
            )
            await db.commit()

    async def get_refresh_token(self, refresh_token: str) -> Optional[RefreshToken]:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM refresh_tokens WHERE refresh_token = ?", (refresh_token,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        if int(time.time()) > row["issued_at"] + row["expires_in"]:
            await self.delete_refresh_token(refresh_token)
            return None
        return RefreshToken(
            refresh_token=row["refresh_token"],
            client_id=row["client_id"],
            user_id=row["user_id"],
            scope=row["scope"],
            issued_at=row["issued_at"],
            expires_in=row["expires_in"],
        )

    async def delete_refresh_token(self, refresh_token: str) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM refresh_tokens WHERE refresh_token = ?", (refresh_token,))
            await db.commit()

    # ------------------------------------------------------------------
    # Authorization codes
    # ------------------------------------------------------------------

    async def save_code(self, auth_code: AuthorizationCode) -> None:
        await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO authorization_codes
                    (code, client_id, redirect_uri, scope, user_id, issued_at, expires_in)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO NOTHING
                """,
                (
                    auth_code.code,
                    auth_code.client_id,
                    auth_code.redirect_uri,
                    auth_code.scope,
                    auth_code.user_id,
                    auth_code.issued_at,
                    auth_code.expires_in,
                ),
            )
            await db.commit()

    async def consume_code(self, code: str) -> Optional[AuthorizationCode]:
        await self.initialize()
        """Return and delete the code (single-use)."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "DELETE FROM authorization_codes WHERE code = ? RETURNING *", (code,)
            ) as cur:
                row = await cur.fetchone()
            await db.commit()
        if not row:
            return None
        if int(time.time()) > row["issued_at"] + row["expires_in"]:
            return None
        return AuthorizationCode(
            code=row["code"],
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            scope=row["scope"],
            user_id=row["user_id"],
            issued_at=row["issued_at"],
            expires_in=row["expires_in"],
        )


#
# Tenant Storage
#

# Store in Tenant private storage
#  Object type:
#    firm:OAuth2Client
#    firm:OAuth2Token
#    firm:OAuth2RefreshToken
#    firm:OAuth2AuthorizationCode


class FirmOAuth2DataStore(OAuth2DataStore):
    _TYPE_CLIENT = "firm:OAuth2Client"
    _TYPE_TOKEN = "firm:OAuth2Token"
    _TYPE_REFRESH_TOKEN = "firm:OAuth2RefreshToken"
    _TYPE_AUTHORIZATION_CODE = "firm:OAuth2AuthorizationCode"

    def __init__(self, tenant: Tenant):
        self._tenant = tenant

    @staticmethod
    def _client_resource_id(client_id: str) -> str:
        return f"urn:firm:oauth2:client:{client_id}"

    @staticmethod
    def _token_resource_id(access_token: str) -> str:
        return f"urn:firm:oauth2:token:{access_token}"

    @staticmethod
    def _refresh_token_resource_id(refresh_token: str) -> str:
        return f"urn:firm:oauth2:refresh-token:{refresh_token}"

    @staticmethod
    def _code_resource_id(code: str) -> str:
        return f"urn:firm:oauth2:code:{code}"

    async def _find_one(self, criteria: Dict[str, str]) -> JSONObject | None:
        return await self._tenant.private_store.query_one(dict(criteria))

    # Clients
    async def get_client(self, client_id: str) -> Optional[OAuth2Client]:
        resource = await self._find_one(
            {
                "type": self._TYPE_CLIENT,
                "client_id": client_id,
            }
        )
        if not resource:
            return None
        return OAuth2Client(
            client_id=str(resource["client_id"]),
            client_secret=resource.get("client_secret"),
            redirect_uris=list(cast(list, resource.get("redirect_uris", []))),
            grant_types=list(cast(list, resource.get("grant_types", []))),
            response_types=list(cast(list, resource.get("response_types", []))),
            scope=str(resource.get("scope", "")),
            token_endpoint_auth_method=str(
                resource.get("token_endpoint_auth_method", "client_secret_basic")
            ),
        )

    async def save_client(self, client: OAuth2Client) -> None:
        # Delete all other client records with this client_id
        existing_clients = await self._tenant.private_store.query({"client_id": client.client_id})
        for existing_client in existing_clients:
            await self._tenant.private_store.remove(str(existing_client["id"]))

        await self._tenant.private_store.put(
            {
                "id": self._client_resource_id(client.client_id),
                "type": self._TYPE_CLIENT,
                "client_id": client.client_id,
                "client_secret": client.client_secret,
                "redirect_uris": client.redirect_uris,
                "grant_types": client.grant_types,
                "response_types": client.response_types,
                "scope": client.scope,
                "token_endpoint_auth_method": client.token_endpoint_auth_method,
            }
        )

    async def authenticate_client(
        self, client_id: str, client_secret: str
    ) -> Optional[OAuth2Client]:
        client = await self.get_client(client_id)
        if client and (not client.client_secret or (client.client_secret == client_secret)):
            return client
        return None

    # Access tokens
    async def save_token(self, token: OAuth2Token) -> None:
        await self._tenant.private_store.put(
            {
                "id": self._token_resource_id(token.access_token),
                "type": self._TYPE_TOKEN,
                "access_token": token.access_token,
                "client_id": token.client_id,
                "user_id": token.user_id,
                "scope": token.scope,
                "issued_at": token.issued_at,
                "expires_in": token.expires_in,
                "token_type": token.token_type,
            }
        )

    async def get_token(self, access_token: str) -> Optional[OAuth2Token]:
        resource = await self._find_one(
            {
                "type": self._TYPE_TOKEN,
                "access_token": access_token,
            }
        )
        if not resource:
            return None
        issued_at = int(str(resource["issued_at"]))
        expires_in = int(str(resource["expires_in"]))
        if int(time.time()) > issued_at + expires_in:
            await self._tenant.private_store.remove(str(resource["id"]))
            return None
        return OAuth2Token(
            access_token=str(resource["access_token"]),
            client_id=str(resource["client_id"]),
            user_id=str(resource["user_id"]),
            scope=str(resource.get("scope", "")),
            issued_at=issued_at,
            expires_in=expires_in,
            token_type=str(resource.get("token_type", "bearer")),
        )

    async def delete_token(self, access_token: str) -> None:
        resource = await self._find_one(
            {
                "type": self._TYPE_TOKEN,
                "access_token": access_token,
            }
        )
        if resource:
            await self._tenant.private_store.remove(str(resource["id"]))

    # Refresh tokens
    async def save_refresh_token(self, token: RefreshToken) -> None:
        await self._tenant.private_store.put(
            {
                "id": self._refresh_token_resource_id(token.refresh_token),
                "type": self._TYPE_REFRESH_TOKEN,
                "refresh_token": token.refresh_token,
                "client_id": token.client_id,
                "user_id": token.user_id,
                "scope": token.scope,
                "issued_at": token.issued_at,
                "expires_in": token.expires_in,
            }
        )

    async def get_refresh_token(self, refresh_token: str) -> Optional[RefreshToken]:
        resource = await self._find_one(
            {
                "type": self._TYPE_REFRESH_TOKEN,
                "refresh_token": refresh_token,
            }
        )
        if not resource:
            return None
        issued_at = int(str(resource["issued_at"]))
        expires_in = int(str(resource["expires_in"]))
        if int(time.time()) > issued_at + expires_in:
            await self._tenant.private_store.remove(str(resource["id"]))
            return None
        return RefreshToken(
            refresh_token=str(resource["refresh_token"]),
            client_id=str(resource["client_id"]),
            user_id=str(resource["user_id"]),
            scope=str(resource.get("scope", "")),
            issued_at=issued_at,
            expires_in=expires_in,
        )

    async def delete_refresh_token(self, refresh_token: str) -> None:
        resource = await self._find_one(
            {
                "type": self._TYPE_REFRESH_TOKEN,
                "refresh_token": refresh_token,
            }
        )
        if resource:
            await self._tenant.private_store.remove(str(resource["id"]))

    # Authorization codes
    async def save_code(self, auth_code: AuthorizationCode) -> None:
        await self._tenant.private_store.put(
            {
                "id": self._code_resource_id(auth_code.code),
                "type": self._TYPE_AUTHORIZATION_CODE,
                "code": auth_code.code,
                "client_id": auth_code.client_id,
                "redirect_uri": auth_code.redirect_uri,
                "scope": auth_code.scope,
                "user_id": auth_code.user_id,
                "issued_at": auth_code.issued_at,
                "expires_in": auth_code.expires_in,
            }
        )

    async def consume_code(self, code: str) -> Optional[AuthorizationCode]:
        """Return and delete the code (single-use)."""
        resource = await self._find_one(
            {
                "type": self._TYPE_AUTHORIZATION_CODE,
                "code": code,
            }
        )
        if not resource:
            return None
        await self._tenant.private_store.remove(str(resource["id"]))
        issued_at = int(str(resource["issued_at"]))
        expires_in = int(str(resource["expires_in"]))
        if int(time.time()) > issued_at + expires_in:
            return None
        return AuthorizationCode(
            code=str(resource["code"]),
            client_id=str(resource["client_id"]),
            redirect_uri=str(resource.get("redirect_uri", "")),
            scope=str(resource.get("scope", "")),
            user_id=str(resource["user_id"]),
            issued_at=issued_at,
            expires_in=expires_in,
        )
