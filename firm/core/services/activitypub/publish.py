"""Publishing an activity as its owner: store it, append it to the outbox, deliver it."""

import logging
from typing import TYPE_CHECKING, Mapping, cast

from firm.core.interfaces import DeliveryService, JSONObject
from firm.core.services.activitypub.collections import add_collection_item
from firm.core.util import resource_id

if TYPE_CHECKING:
    from firm.core.services.activitypub.support import FirmContext

log = logging.getLogger(__name__)


async def outbox_send(
    context: "FirmContext",
    activity: JSONObject,
    outbox_uri: str,
    delivery_service: DeliveryService,
) -> None:
    """Store what the activity carries, then the activity, then send it to its addressees.

    Everything goes through ``keep``: a published activity and the object it holds are
    documents like any other, normalized to what a store holds and placed where the owner
    of each id keeps it.
    """
    object_ = activity.get("object")
    if isinstance(object_, Mapping):
        await context.keep(cast(JSONObject, object_))
    await context.keep(activity)
    await add_collection_item(context.tenant.public_store, outbox_uri, resource_id(activity))
    report = await delivery_service.deliver(context.tenant, context.tenants, activity)
    for failure in report.failed:
        log.error(
            "Delivery of %s to %s failed after %d attempts: %s",
            resource_id(activity),
            failure.inbox or "the receiving instance",
            failure.attempts,
            failure.reason,
        )
