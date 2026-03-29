"""
Lambda handler for the order service.

This module contains two handler functions, each mapped to a different Lambda:

1. api_handler -- Handles HTTP requests from API Gateway (via the BFF Lambda).
   Routes: POST /orders, GET /orders/{id}, PUT /orders/{id}/cancel

2. event_handler -- Handles EventBridge events from other services.
   Events: inventory.reserved, inventory.failed, payment.completed,
           payment.failed, inventory.released

The handler extracts the correlation ID (from headers or event metadata),
sets up the logger, and delegates to the saga engine or models layer.
"""

import json
import traceback

from shared.exceptions import (
    BaseServiceError,
    InvalidOrderStateError,
    OrderNotFoundError,
)
from shared.logger import get_logger

from . import saga
from .compensation import handle_inventory_released
from .models import create_order, create_saga_state, get_order, update_order_status

logger = get_logger("order-service")


# ---------------------------------------------------------------------------
# Helper: Build an API Gateway response
# ---------------------------------------------------------------------------


def _response(status_code: int, body: dict) -> dict:
    """Build a properly formatted API Gateway proxy response.

    API Gateway expects this exact shape -- statusCode, headers, and a
    JSON-serialized body string.
    """
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            # CORS headers -- allow the frontend to call the API
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Correlation-Id",
        },
        "body": json.dumps(body, default=str),
    }


# ---------------------------------------------------------------------------
# API Handler -- processes HTTP requests from API Gateway
# ---------------------------------------------------------------------------


def api_handler(event, context):
    """Lambda entry point for API Gateway requests.

    API Gateway sends the request as a proxy event with httpMethod,
    pathParameters, body, and headers fields.

    Args:
        event: API Gateway proxy event.
        context: Lambda context (contains request ID, remaining time, etc.).

    Returns:
        API Gateway proxy response dict.
    """
    # Extract correlation ID from request headers (injected by M1's BFF Lambda)
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
        # --- Route: POST /orders ---
        if http_method == "POST" and path == "/orders":
            return _handle_create_order(event, correlation_id)

        # --- Route: GET /orders/{id} ---
        if http_method == "GET" and "id" in path_params:
            return _handle_get_order(path_params["id"])

        # --- Route: PUT /orders/{id}/cancel ---
        if http_method == "PUT" and path.endswith("/cancel") and "id" in path_params:
            return _handle_cancel_order(path_params["id"], correlation_id)

        # No matching route
        return _response(404, {"error": "Route not found"})

    except BaseServiceError as e:
        # Known business errors -- return structured error response
        logger.warn("Business error", error_code=e.error_code, error_message=e.message)
        return _response(e.status_code, e.to_dict())

    except Exception as e:
        # Unexpected errors -- log the full traceback for debugging
        logger.error("Unhandled error", error=str(e), traceback=traceback.format_exc())
        return _response(500, {"error_code": "INTERNAL_ERROR", "message": "Internal server error"})


def _handle_create_order(event: dict, correlation_id: str) -> dict:
    """Process a POST /orders request.

    1. Parse and validate the request body.
    2. Create the order record in DynamoDB (status: PENDING).
    3. Initialize the saga state.
    4. Start the saga (publishes order.created event -> M4 inventory service picks it up).

    Expected request body:
    {
        "user_id": "usr_abc123",
        "items": [
            {"product_id": "prod_001", "quantity": 2, "unit_price": 29.99}
        ],
        "shipping_address": {"street": "...", "city": "...", "state": "...", "zip": "..."}
    }
    """
    body = json.loads(event.get("body", "{}"))

    # Validate required fields
    user_id = body.get("user_id")
    items = body.get("items")
    if not user_id or not items:
        return _response(400, {
            "error_code": "VALIDATION_ERROR",
            "message": "user_id and items are required",
        })

    # Validate each item has required fields
    for item in items:
        if not all(k in item for k in ("product_id", "quantity", "unit_price")):
            return _response(400, {
                "error_code": "VALIDATION_ERROR",
                "message": "Each item must have product_id, quantity, and unit_price",
            })

    # Create the order and saga state in DynamoDB
    order = create_order(
        user_id=user_id,
        items=items,
        shipping_address=body.get("shipping_address"),
    )
    create_saga_state(order["order_id"])

    # Start the saga -- this publishes order.created to EventBridge
    saga.start_saga(order, correlation_id=correlation_id)

    logger.info("Order created and saga started", order_id=order["order_id"])

    return _response(201, {
        "message": "Order created",
        "order": order,
    })


def _handle_get_order(order_id: str) -> dict:
    """Process a GET /orders/{id} request."""
    order = get_order(order_id)
    if not order:
        raise OrderNotFoundError(order_id)

    return _response(200, {"order": order})


def _handle_cancel_order(order_id: str, correlation_id: str) -> dict:
    """Process a PUT /orders/{id}/cancel request.

    Users can cancel an order only if it hasn't been confirmed yet.
    If inventory was already reserved, this triggers the compensation flow
    to release the reserved stock.
    """
    order = get_order(order_id)
    if not order:
        raise OrderNotFoundError(order_id)

    current_status = order["status"]

    # Can only cancel orders that are still in progress
    if current_status == "CANCELLED":
        return _response(409, {
            "error_code": "ORDER_ALREADY_CANCELLED",
            "message": f"Order {order_id} is already cancelled",
        })

    if current_status == "CONFIRMED":
        raise InvalidOrderStateError(order_id, current_status, "cancel")

    # If inventory was reserved and payment is processing, we need to
    # compensate (release inventory)
    if current_status in ("INVENTORY_RESERVED", "PAYMENT_PROCESSING"):
        from .models import get_saga_state

        saga_state = get_saga_state(order_id)
        from . import compensation
        from .models import transition_saga_state

        # Transition saga to a failure state first, then compensate
        transition_saga_state(
            order_id=order_id,
            from_state=saga_state["current_state"],
            to_state="PAYMENT_FAILED",
            reason="User requested cancellation",
        )
        compensation.compensate_inventory(
            order_id=order_id,
            saga_state=saga_state,
            reason="User requested cancellation",
            correlation_id=correlation_id,
        )
        return _response(200, {
            "message": "Cancellation initiated -- inventory release in progress",
            "order_id": order_id,
        })

    # If inventory hasn't been reserved yet, just cancel directly
    update_order_status(order_id, "CANCELLED", cancellation_reason="User requested cancellation")

    logger.info("Order cancelled by user", order_id=order_id)

    return _response(200, {
        "message": "Order cancelled",
        "order_id": order_id,
    })


# ---------------------------------------------------------------------------
# Event Handler -- processes EventBridge events from other services
# ---------------------------------------------------------------------------


def event_handler(event, context):
    """Lambda entry point for EventBridge events.

    EventBridge delivers events with detail-type and detail fields.
    We route based on detail-type to the appropriate saga handler.

    Args:
        event: EventBridge event (contains detail-type and detail).
        context: Lambda context.
    """
    detail_type = event.get("detail-type", "")
    detail = event.get("detail", {})

    # Parse the event envelope -- our events wrap the payload in metadata + data
    if isinstance(detail, str):
        detail = json.loads(detail)

    metadata = detail.get("metadata", {})
    data = detail.get("data", {})
    correlation_id = metadata.get("correlation_id", context.aws_request_id)

    logger.set_correlation_id(correlation_id)
    logger.info("Event received", detail_type=detail_type, order_id=data.get("order_id"))

    try:
        # --- Event: inventory.reserved ---
        # Published by M4 inventory service after reserving stock
        if detail_type == "inventory.reserved":
            saga.handle_inventory_reserved(
                order_id=data["order_id"],
                reservation_id=data.get("reservation_id", ""),
                correlation_id=correlation_id,
            )

        # --- Event: inventory.failed ---
        # Published by M4 inventory service when stock is unavailable
        elif detail_type == "inventory.failed":
            saga.handle_inventory_failed(
                order_id=data["order_id"],
                reason=data.get("reason", "Inventory unavailable"),
                correlation_id=correlation_id,
            )

        # --- Event: payment.completed ---
        # Published by payment service after successful Stripe charge
        elif detail_type == "payment.completed":
            saga.handle_payment_completed(
                order_id=data["order_id"],
                payment_id=data["payment_id"],
                charge_id=data["charge_id"],
                amount=data["amount"],
                correlation_id=correlation_id,
            )

        # --- Event: payment.failed ---
        # Published by payment service when Stripe charge is declined
        elif detail_type == "payment.failed":
            saga.handle_payment_failed(
                order_id=data["order_id"],
                reason=data.get("reason", "Unknown payment failure"),
                correlation_id=correlation_id,
            )

        # --- Event: inventory.released ---
        # Published by M4 inventory service after releasing reserved stock (compensation)
        elif detail_type == "inventory.released":
            handle_inventory_released(
                order_id=data["order_id"],
                correlation_id=correlation_id,
            )

        else:
            logger.warn("Unknown event type -- ignoring", detail_type=detail_type)

    except Exception as e:
        # Log the error -- the event will be retried by EventBridge or sent to DLQ
        logger.error(
            "Error processing event",
            detail_type=detail_type,
            order_id=data.get("order_id"),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        # Re-raise so Lambda marks the invocation as failed (triggers retry/DLQ)
        raise
