"""Publishing an activity as its owner: store it, append it to the outbox, deliver it."""

import logging
from typing import Mapping, cast

from firm.core.interfaces import DeliveryService, JSONObject, Tenant
from firm.core.services.activitypub.collections import add_collection_item
from firm.core.util import get_id, is_local_uri, resource_id

log = logging.getLogger(__name__)


async def outbox_send(
    tenant: Tenant,
    tenants: Mapping[str, Tenant],
    activity: JSONObject,
    outbox_uri: str,
    delivery_service: DeliveryService,
) -> None:
    object_ = activity.get("object")
    if isinstance(object_, Mapping):
        object_uri = get_id(object_)
        if object_uri is not None and is_local_uri(tenant, str(object_uri)):
            await tenant.public_store.put(cast(JSONObject, object_))
    await tenant.public_store.put(activity)
    await add_collection_item(tenant.public_store, outbox_uri, resource_id(activity))
    report = await delivery_service.deliver(tenant, tenants, activity)
    for failure in report.failed:
        log.error(
            "Delivery of %s to %s failed after %d attempts: %s",
            resource_id(activity),
            failure.inbox or "the receiving instance",
            failure.attempts,
            failure.reason,
        )
