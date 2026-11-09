"""Publishing an activity as its owner: store it, append it to the outbox, deliver it."""

import logging
from typing import TYPE_CHECKING, Mapping, cast

from firm.core.interfaces import JSONObject
from firm.core.services.activitypub.collections import add_collection_item
from firm.core.util import get_id, resource_id

if TYPE_CHECKING:
    from firm.core.services.activitypub.support import FirmBoxContext

log = logging.getLogger(__name__)


async def outbox_send(
    context: "FirmBoxContext",
    activity: JSONObject,
) -> None:
    """Store what the activity carries, then the activity, then send it to its addressees.

    An activity published this way is published as the box owner: its outbox lists the
    activity, and its identity is who the delivery is from. The request's principal is a
    different party - an ``Accept`` is published while the principal is still the remote
    actor who followed - so the sender is taken from the box, never from the caller, and
    the store is asked who the document is stored as rather than who caused the request.

    Anything the activity merely names stays a reference, as everywhere else.
    """
    sender = context.box_owner
    sender_uri = get_id(sender)
    outbox_uri = resource_id(sender.get("outbox"))
    if not sender_uri or not outbox_uri:
        raise NotImplementedError("Sender has no id or no outbox")
    object_ = activity.get("object")
    if isinstance(object_, Mapping):
        await context.keep(cast(JSONObject, object_), actor_uri=sender_uri)
    await context.keep(activity, actor_uri=sender_uri)
    await add_collection_item(context.tenant.public_store, outbox_uri, resource_id(activity))
    report = await context.delivery_service.deliver(context.tenant, context.tenants, activity)
    for failure in report.failed:
        log.error(
            "Delivery of %s to %s failed after %d attempts: %s",
            resource_id(activity),
            failure.inbox or "the receiving instance",
            failure.attempts,
            failure.reason,
        )
