import os
import json
import uuid
import boto3
from datetime import datetime, timezone

from repository import put_shipment, get_shipment_by_order
from common.logger import get_logger

logger = get_logger(__name__)

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")
events_client = boto3.client("events")


def _generate_tracking_number() -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = str(uuid.uuid4().int)[:4].zfill(4)
    return f"MOCK-{date_str}-{suffix}"


def create_shipment(order_id: str, email: str, shipping_address: dict, items: list):
    logger.info(f"Creating shipment for order {order_id}")

    # Idempotency: if a shipment already exists for this order, republish and return
    existing = get_shipment_by_order(order_id)
    if existing:
        logger.warning(
            f"Shipment already exists for order {order_id} ({existing['shipmentId']}), "
            f"republishing ShipmentCreated"
        )
        _publish_shipment_created(existing)
        return existing

    shipment_id = f"shp_{uuid.uuid4().hex[:8]}"
    tracking_number = _generate_tracking_number()
    created_at = datetime.now(timezone.utc).isoformat()

    item = {
        "shipmentId": shipment_id,
        "orderId": order_id,
        "email": email,
        "carrier": "UPS_MOCK",
        "trackingNumber": tracking_number,
        "status": "LABEL_CREATED",
        "shippingAddress": shipping_address,
        "items": items,
        "createdAt": created_at,
    }

    try:
        put_shipment(item)
    except Exception as e:
        logger.error(f"Failed to store shipment for order {order_id}: {e}")
        _publish_event("ShipmentCreationFailed", {
            "orderId": order_id,
            "reason": "failed to store shipment",
        })
        raise

    _publish_shipment_created(item)
    logger.info(f"Shipment {shipment_id} created for order {order_id}, tracking {tracking_number}")
    return item


def _publish_shipment_created(shipment: dict):
    _publish_event("ShipmentCreated", {
        "shipmentId": shipment["shipmentId"],
        "orderId": shipment["orderId"],
        "email": shipment["email"],
        "carrier": shipment["carrier"],
        "trackingNumber": shipment["trackingNumber"],
        "status": shipment["status"],
        "items": shipment["items"],
    })


def _publish_event(detail_type: str, detail: dict):
    try:
        events_client.put_events(Entries=[{
            "Source": "shipping-service",
            "DetailType": detail_type,
            "Detail": json.dumps(detail),
            "EventBusName": EVENT_BUS_NAME,
        }])
        logger.info(f"Published event: {detail_type}")
    except Exception as e:
        logger.error(f"Failed to publish event {detail_type}: {e}")
