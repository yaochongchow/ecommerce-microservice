import json
from common.event_utils import unwrap_event, get_detail_type, get_detail
from common.logger import get_logger
from service import _publish_event

logger = get_logger(__name__)

# Maps each consumed event type to the failure event to publish and which detail
# fields to forward so the consumer can identify what failed.
_FAILURE_MAP = {
    "ProductCreated": {
        "event": "InventoryInitializationFailed",
        "fields": ["productId"],
    },
    "ProductRestocked": {
        "event": "ProductRestockFailed",
        "fields": ["productId", "quantity"],
    },
    "OrderCreated": {
        "event": "InventoryReservationFailed",
        "fields": ["orderId", "items"],
    },
    "OrderCanceled": {
        "event": "InventoryReleaseFailed",
        "fields": ["orderId"],
    },
    "CompensateInventory": {
        "event": "InventoryReleaseFailed",
        "fields": ["orderId"],
    },
    "ShipmentCreated": {
        "event": "InventoryFulfillmentFailed",
        "fields": ["orderId"],
    },
    "OrderReturned": {
        "event": "InventoryRestockFailed",
        "fields": ["orderId", "returnId", "items"],
    },
}


def lambda_handler(event: dict, context):
    logger.info(f"DLQ handler received event: {json.dumps(event)}")

    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            eb_event = unwrap_event(body) if "Records" in body else body
            detail_type = get_detail_type(eb_event)
            detail = get_detail(eb_event)

            mapping = _FAILURE_MAP.get(detail_type)
            if mapping is None:
                logger.warning(f"No failure mapping for detail-type '{detail_type}', publishing GenericProcessingFailed")
                _publish_event("GenericProcessingFailed", {
                    "detailType": detail_type,
                    "reason": "processing failed after max retries",
                })
                continue

            payload = {k: detail[k] for k in mapping["fields"] if k in detail}
            payload["reason"] = "processing failed after max retries"
            _publish_event(mapping["event"], payload)
            logger.info(f"Published {mapping['event']} for failed {detail_type}")

        except Exception as e:
            logger.error(f"DLQ handler failed to process record: {e}")
