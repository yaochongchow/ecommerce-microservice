import json
from common.event_utils import unwrap_event, get_detail_type, get_detail
from common.logger import get_logger
from service import (
    create_product, restock_product, reserve_inventory, release_inventory,
    fulfill_inventory, restock_inventory,
)

logger = get_logger(__name__)


def lambda_handler(event: dict, context):
    logger.info(f"Received event: {json.dumps(event)}")
    event = unwrap_event(event)

    detail_type = get_detail_type(event)
    detail = get_detail(event)

    try:
        if detail_type == "ProductCreated":
            create_product(detail["productId"], int(detail["stock"]))
            return {"status": "created", "productId": detail["productId"]}

        elif detail_type == "ProductRestocked":
            restock_product(detail["productId"], int(detail["quantity"]))
            return {"status": "restocked", "productId": detail["productId"]}

        elif detail_type == "OrderCreated":
            reserve_inventory(detail["orderId"], detail["items"])
            return {"status": "reserved", "orderId": detail["orderId"]}

        elif detail_type == "OrderCanceled":
            # Items and quantities come from reservations table — no need to read event items
            release_inventory(detail["orderId"])
            return {"status": "released", "orderId": detail["orderId"]}

        elif detail_type == "ShipmentCreated":
            # Quantities come from reservations table — items in event are not needed
            fulfill_inventory(detail["orderId"])
            return {"status": "fulfilled", "orderId": detail["orderId"]}

        elif detail_type == "OrderReturned":
            restock_inventory(detail["orderId"], detail["returnId"], detail.get("items", []))
            return {"status": "restocked", "orderId": detail["orderId"]}

        else:
            logger.warning(f"Unhandled detail-type: {detail_type}")
            return {"status": "ignored", "detail-type": detail_type}

    except ValueError as e:
        logger.error(f"Business error: {e}")
        return {"status": "error", "message": str(e)}

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise
