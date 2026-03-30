"""
Lambda handlers for the order service.

1. api_handler — HTTP requests from API Gateway (via BFF)
2. event_handler — EventBridge events via SQS from other services
"""

import json
import traceback

from common.event_utils import unwrap_event, get_detail_type, get_detail
from shared.exceptions import BaseServiceError, InvalidOrderStateError, OrderNotFoundError
from shared.logger import get_logger

from . import saga
from .compensation import handle_inventory_released
from .models import create_order, create_saga_state, get_order, update_order_status

logger = get_logger("order-service")


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Correlation-Id",
        },
        "body": json.dumps(body, default=str),
    }


def api_handler(event, context):
    headers = event.get("headers") or {}
    correlation_id = headers.get("x-correlation-id") or headers.get(
        "X-Correlation-Id", context.aws_request_id
    )
    logger.set_correlation_id(correlation_id)

    http_method = event.get("httpMethod", "")
    path = event.get("path", "")
    path_params = event.get("pathParameters") or {}

    logger.info("API request received", method=http_method, path=path)

    try:
        if http_method == "POST" and path == "/orders":
            return _handle_create_order(event, correlation_id)
        if http_method == "GET" and "id" in path_params:
            return _handle_get_order(path_params["id"])
        if http_method == "PUT" and path.endswith("/cancel") and "id" in path_params:
            return _handle_cancel_order(path_params["id"], correlation_id)
        return _response(404, {"error": "Route not found"})

    except BaseServiceError as e:
        logger.warn("Business error", error_code=e.error_code, error_message=e.message)
        return _response(e.status_code, e.to_dict())
    except Exception as e:
        logger.error("Unhandled error", error=str(e), traceback=traceback.format_exc())
        return _response(500, {"error_code": "INTERNAL_ERROR", "message": "Internal server error"})


def _handle_create_order(event, correlation_id):
    body = json.loads(event.get("body", "{}"))
    user_id = body.get("user_id")
    items = body.get("items")
    if not user_id or not items:
        return _response(400, {"error_code": "VALIDATION_ERROR", "message": "user_id and items are required"})

    for item in items:
        if not all(k in item for k in ("product_id", "quantity", "unit_price")):
            return _response(400, {"error_code": "VALIDATION_ERROR", "message": "Each item must have product_id, quantity, and unit_price"})

    order = create_order(user_id=user_id, items=items, shipping_address=body.get("shipping_address"))
    create_saga_state(order["order_id"])
    saga.start_saga(order, correlation_id=correlation_id)
    logger.info("Order created and saga started", order_id=order["order_id"])
    return _response(201, {"message": "Order created", "order": order})


def _handle_get_order(order_id):
    order = get_order(order_id)
    if not order:
        raise OrderNotFoundError(order_id)
    return _response(200, {"order": order})


def _handle_cancel_order(order_id, correlation_id):
    order = get_order(order_id)
    if not order:
        raise OrderNotFoundError(order_id)

    current_status = order["status"]
    if current_status == "CANCELLED":
        return _response(409, {"error_code": "ORDER_ALREADY_CANCELLED", "message": f"Order {order_id} is already cancelled"})
    if current_status == "CONFIRMED":
        raise InvalidOrderStateError(order_id, current_status, "cancel")

    if current_status in ("INVENTORY_RESERVED", "PAYMENT_PROCESSING"):
        from .models import get_saga_state
        saga_state = get_saga_state(order_id)
        from . import compensation
        from .models import transition_saga_state
        transition_saga_state(order_id=order_id, from_state=saga_state["current_state"], to_state="PAYMENT_FAILED", reason="User requested cancellation")
        compensation.compensate_inventory(order_id=order_id, saga_state=saga_state, reason="User requested cancellation", correlation_id=correlation_id)
        return _response(200, {"message": "Cancellation initiated", "order_id": order_id})

    update_order_status(order_id, "CANCELLED", cancellation_reason="User requested cancellation")
    logger.info("Order cancelled by user", order_id=order_id)
    return _response(200, {"message": "Order cancelled", "order_id": order_id})


def event_handler(event, context):
    """Handles EventBridge events routed via SQS."""
    eb_event = unwrap_event(event)
    detail_type = get_detail_type(eb_event)
    detail = get_detail(eb_event)

    correlation_id = detail.get("correlationId", context.aws_request_id)
    logger.set_correlation_id(correlation_id)
    logger.info("Event received", detail_type=detail_type, order_id=detail.get("orderId"))

    try:
        if detail_type == "InventoryReserved":
            saga.handle_inventory_reserved(
                order_id=detail["orderId"],
                reservation_id=detail.get("reservationId", ""),
                correlation_id=correlation_id,
            )
        elif detail_type == "InventoryReservationFailed":
            saga.handle_inventory_failed(
                order_id=detail["orderId"],
                reason=detail.get("reason", "Inventory unavailable"),
                correlation_id=correlation_id,
            )
        elif detail_type == "PaymentSucceeded":
            saga.handle_payment_completed(
                order_id=detail["orderId"],
                payment_id=detail["paymentId"],
                charge_id=detail["chargeId"],
                amount=detail["amount"],
                correlation_id=correlation_id,
            )
        elif detail_type == "PaymentFailed":
            saga.handle_payment_failed(
                order_id=detail["orderId"],
                reason=detail.get("reason", "Unknown payment failure"),
                correlation_id=correlation_id,
            )
        elif detail_type == "InventoryReleased":
            handle_inventory_released(
                order_id=detail["orderId"],
                correlation_id=correlation_id,
            )
        else:
            logger.warn("Unknown event type", detail_type=detail_type)

    except Exception as e:
        logger.error("Error processing event", detail_type=detail_type, order_id=detail.get("orderId"), error=str(e), traceback=traceback.format_exc())
        raise
