import json
from common.event_utils import unwrap_event, get_detail_type, get_detail
from common.logger import get_logger
from service import create_shipment

logger = get_logger(__name__)


def lambda_handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")
    event = unwrap_event(event)

    detail_type = get_detail_type(event)
    detail = get_detail(event)

    try:
        if detail_type == "OrderConfirmed":
            shipment = create_shipment(
                order_id=detail["orderId"],
                email=detail.get("email", "customer@example.com"),
                shipping_address=detail.get("shippingAddress", {}),
                items=detail.get("items", []),
            )
            return {"status": "created", "shipmentId": shipment["shipmentId"]}
        else:
            logger.warning(f"Unhandled detail-type: {detail_type}")
            return {"status": "ignored", "detail-type": detail_type}

    except KeyError as e:
        logger.error(f"Missing field in event: {e}")
        return {"status": "error", "message": f"Missing field: {e}"}
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise
