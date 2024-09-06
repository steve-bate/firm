import logging
from typing import cast
from urllib.parse import urlparse

from firm.interfaces import (
    FIRM_NS,
    AuthorizationDecision,
    AuthorizationService,
    Identity,
    JSONObject,
    ResourceStore,
)
from firm.util import (
    AP_PUBLIC_URIS,
    get_id,
    has_value,
    is_actor_collection,
    is_actor_object,
    is_recipient,
    is_type,
    is_type_any,
    resource_id,
)

log = logging.getLogger(__name__)


def is_attributed_user(principal: Identity, resource: JSONObject) -> bool:
    return has_value(resource, "attributedTo", principal.uri)


def is_activity_actor(principal: Identity, resource: JSONObject) -> bool:
    actors = resource.get("actor")
    if actors:
        if isinstance(actors, str):
            return actors == principal.uri
        elif isinstance(actors, dict):
            return actors["id"] == principal.uri
        # FIXME What about a list of actor objects, etc.?
        elif isinstance(actors, list):
            return principal.uri in actors

    attribution = get_id(resource.get("attributedTo"))
    if attribution:
        if isinstance(attribution, str):
            return attribution == principal.uri
        else:
            return principal.uri in attribution
    return False


async def is_outbox(store: ResourceStore, resource: JSONObject | str) -> bool:
    return await store.query_one({"outbox": resource_id(resource)}) is not None


async def is_inbox(store: ResourceStore, resource: JSONObject | str) -> bool:
    return await store.query_one({"inbox": resource_id(resource)}) is not None


async def get_box_owner(
    store: ResourceStore, resource: JSONObject | str
) -> JSONObject | None:
    # Can't use type-safe Resource in isinstance check :-(
    resource_uri = resource_id(resource)
    return await store.query_one({"inbox": resource_uri}) or await store.query_one(
        {"outbox": resource_uri}
    )


def is_public(resource: JSONObject) -> bool:
    for key in ["to", "cc", "bto", "bcc", "audience"]:
        if key in resource:
            for uri in AP_PUBLIC_URIS:
                if has_value(resource, key, uri):
                    return True
    return False


class CoreAuthorizationService(AuthorizationService):
    # Placeholder for extensible authorization behavior
    next_auth: AuthorizationService | None = None

    def __init__(self, server_prefix: str, store: ResourceStore):
        self._prefix = server_prefix
        self._store = store

    async def is_get_authorized(
        self, principal: Identity | None, resource: JSONObject
    ) -> AuthorizationDecision:
        request_actor_uri = principal.uri if principal else None

        log.debug(
            f"GET obj={resource['id']}, principal={request_actor_uri or 'anonymous'}"
        )

        # TODO need to revisit authorized fetch
        if request_actor_uri:
            blocked = await self._is_blocked(self._prefix, request_actor_uri)
            if not blocked.authorized:
                return blocked

        if is_public(resource):
            return AuthorizationDecision(True, "public object")

        if is_actor_object(resource):
            # TODO Consider "private" or "local" actors
            return AuthorizationDecision(True, "allow actor access")

        if await is_outbox(self._store, resource):
            # TODO Consider optional authorized fetch for non-actor resources
            return AuthorizationDecision(True, "public outbox read is allowed")

        if await is_inbox(self._store, resource):
            box_owner = await get_box_owner(self._store, resource)
            if box_owner and box_owner["id"] == request_actor_uri:
                return AuthorizationDecision(True, "in/outbox access allowed for owner")
            else:
                return AuthorizationDecision(
                    False,
                    (
                        "anonymous inbox read not allowed"
                        if request_actor_uri is None
                        else "inbox read allowed only for owner"
                    ),
                )

        if request_actor_uri and is_recipient(resource, request_actor_uri):
            return AuthorizationDecision(True, "object recipient access is allowed")

        if principal:
            if is_attributed_user(principal, resource):
                return AuthorizationDecision(True, "object attributed to user")

            if is_activity_actor(principal, resource):
                return AuthorizationDecision(True, "activity actor is user")
        else:
            return AuthorizationDecision(False, "authentication required", 401)
        # TODO authorization -- is_affected_by ?
        # TODO authorization -- is_mention ?

        return AuthorizationDecision(False, "no authorization")

    async def _is_blocked(
        self, principal: str, request_actor_uri: str
    ) -> AuthorizationDecision:
        if blocks := await self._store.query_one(
            {
                "type": FIRM_NS.Blocks.value,
                "attributedTo": principal,
            }
        ):
            # Domain blocks
            if blocked_domains := cast(list, blocks.get(FIRM_NS.blockedDomain.value)):
                parsed_uri = urlparse(request_actor_uri)
                if parsed_uri.hostname in blocked_domains:
                    return AuthorizationDecision(
                        False, "inbox post is blocked for domain"
                    )

            # Actor blocks at instance level
            if blocked_actors := cast(list, blocks.get(FIRM_NS.blockedActor.value)):
                if request_actor_uri in blocked_actors:
                    return AuthorizationDecision(
                        False, "inbox post is blocked for actor"
                    )
        return AuthorizationDecision(True, "Not blocked")

    async def is_post_authorized(
        self, principal: Identity | None, box_type: str, box_uri: str
    ) -> AuthorizationDecision:
        request_actor_uri = principal.uri if principal else None

        if request_actor_uri:
            if box_type == "inbox":
                # Allow any authenticated, non-blocked user to post to inbox

                block_decision = await self._is_blocked(self._prefix, request_actor_uri)
                if not block_decision.authorized:
                    return block_decision

                return AuthorizationDecision(
                    True, "authenticated users can post to inbox"
                )

            if box_type == "outbox" and principal:
                if principal.actor["outbox"] == box_uri:
                    # TODO Consider delegated authority?
                    return AuthorizationDecision(True, "outbox owner can post to it")
                else:
                    return AuthorizationDecision(
                        False, "only outbox owner can post to it"
                    )

        return AuthorizationDecision(False, "authentication required", 401)

    async def is_activity_authorized(
        self, principal: Identity | None, activity: JSONObject
    ) -> AuthorizationDecision:
        # TODO Move this to more general message validation
        # if "actor" not in activity:
        #     return AuthorizationDecision(False, "Missing activity actor", 400)

        # TODO it's not completely clear how to handle actor-level activity authz
        # We may need to do at the actor-specific delivery stage for incoming activities
        #
        # if principal:
        #     block_decision = await self._is_blocked(
        #         principal.uri, principal.uri)
        #     )
        #     if not block_decision.authorized:
        #         return block_decision

        if is_type_any(activity, ["Add", "Remove"]):
            if "object" not in activity:
                return AuthorizationDecision(False, "Missing activity object", 400)
            if "target" not in activity:
                return AuthorizationDecision(False, "Missing activity target", 400)
            target = await self._store.get(resource_id(activity["target"]))
            if (
                principal
                and target
                and (
                    is_public(target)
                    or is_attributed_user(principal, target)
                    # TODO This may not be needed if attributeTo is set correctly.
                    or is_actor_collection(principal.actor, resource_id(target["id"]))
                )
            ):
                return AuthorizationDecision(
                    True, "Public/owned collection changes allowed"
                )
        elif is_type_any(
            activity,
            ["Announce", "Like", "Follow", "Accept", "Reject", "Create", "Block"],
        ):
            return AuthorizationDecision(True, "authorized")
        elif is_type(activity, "Undo"):
            if "object" not in activity:
                return AuthorizationDecision(True, "Missing activity")
            undone_activity_uri = resource_id(activity["object"])
            undone_activity = await self._store.get(undone_activity_uri)
            if undone_activity and is_type_any(
                undone_activity, ["Follow", "Announce", "Like"]
            ):
                if "actor" not in undone_activity:
                    return AuthorizationDecision(True, "Missing actor")
                if principal and principal.uri == get_id(undone_activity["actor"]):
                    return AuthorizationDecision(True, "Same origin/actor")
        elif is_type_any(activity, ["Update", "Delete"]):
            if "object" not in activity:
                # TODO Consider specifying HTTP status_code in auth decision
                return AuthorizationDecision(True, "Missing activity")
            obj = await self._store.get(resource_id(activity["object"]))
            if not obj:
                return AuthorizationDecision(False, "Object not found", 404)
            if principal and is_attributed_user(principal, obj):
                return AuthorizationDecision(True, "Attributed delete allowed")
        else:
            if self.next_auth:
                return await self.next_auth.is_activity_authorized(principal, activity)
            else:
                # Create a fake Create activity and see if that would be authorized
                if await self.is_activity_authorized(
                    principal, {"type": "Create", "object": activity}
                ):
                    return AuthorizationDecision(True, "Implicit create is allowed")
                else:
                    raise NotImplementedError(f"{principal} {activity}")
        return AuthorizationDecision(False, "not authorized")
