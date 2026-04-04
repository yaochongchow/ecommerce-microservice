import json
from common.event_utils import unwrap_event, get_detail_type, get_detail
from common.logger import get_logger
from service import notify_payment_succeeded, notify_shipment_created, notify_order_canceled, notify_payment_refunded

logger = get_logger(__name__)


def lambda_handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")
    event = unwrap_event(event)

    detail_type = get_detail_type(event)
    detail = get_detail(event)

    try:
        if detail_type == "OrderConfirmed":
            notify_payment_succeeded(detail)
            return {"status": "sent", "type": "order_confirmation"}

        elif detail_type == "ShipmentCreated":
            notify_shipment_created(detail)
            return {"status": "sent", "type": "shipment_notification"}

        elif detail_type == "OrderCanceled":
            notify_order_canceled(detail)
            return {"status": "sent", "type": "order_cancellation"}

        elif detail_type == "PaymentRefunded":
            notify_payment_refunded(detail)
            return {"status": "sent", "type": "payment_refund_notification"}

        else:
            logger.warning(f"Unhandled detail-type: {detail_type}")
            return {"status": "ignored", "detail-type": detail_type}

    except KeyError as e:
        logger.error(f"Missing field in event: {e}")
        return {"status": "error", "message": f"Missing field: {e}"}
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise
