"""
Saga orchestrator for the order fulfillment workflow.

Saga flow (happy path):
    PENDING -> INVENTORY_RESERVING -> INVENTORY_RESERVED -> PAYMENT_PROCESSING -> CONFIRMED

Saga flow (inventory fails):
    PENDING -> INVENTORY_RESERVING -> INVENTORY_FAILED -> CANCELLED

Saga flow (payment fails after inventory reserved):
    PENDING -> ... -> PAYMENT_FAILED -> COMPENSATING -> CANCELLED
"""

from shared.events import (
    build_order_cancelled_event,
    build_order_confirmed_event,
    build_order_created_event,
    build_order_ready_for_payment_event,
    publish_event,
)
from shared.logger import get_logger

from . import compensation
from .models import get_order, get_saga_state, transition_saga_state, update_order_status

logger = get_logger("order-service-saga")

ORDER_SOURCE = "order-service"


def start_saga(order, correlation_id=None):
    logger.set_correlation_id(correlation_id)
    order_id = order["order_id"]

    saga_state = transition_saga_state(
        order_id=order_id, from_state="PENDING", to_state="INVENTORY_RESERVING",
        reason="Initiating inventory reservation",
    )

    event_data = build_order_created_event(
        order_id=order_id, user_id=order["user_id"], items=order["items"],
        total_amount=float(order["total_amount"]), currency=order.get("currency", "USD"),
        idempotency_key=order.get("idempotency_key"),
    )
    publish_event("OrderCreated", event_data, source=ORDER_SOURCE, correlation_id=correlation_id)

    logger.info("Saga started", order_id=order_id, saga_state="INVENTORY_RESERVING")
    return saga_state


def handle_inventory_reserved(order_id, reservation_id, correlation_id=None):
    logger.set_correlation_id(correlation_id)

    saga_state = transition_saga_state(
        order_id=order_id, from_state="INVENTORY_RESERVING", to_state="INVENTORY_RESERVED",
        reason=f"Inventory reserved: {reservation_id}", reservation_id=reservation_id,
    )
    update_order_status(order_id, "INVENTORY_RESERVED", reservation_id=reservation_id)

    saga_state = transition_saga_state(
        order_id=order_id, from_state="INVENTORY_RESERVED", to_state="PAYMENT_PROCESSING",
        reason="Inventory reserved — requesting payment",
    )

    order = get_order(order_id)
    event_data = build_order_ready_for_payment_event(
        order_id=order_id, user_id=order["user_id"], items=order["items"],
        total_amount=float(order["total_amount"]), currency=order.get("currency", "USD"),
        idempotency_key=order.get("idempotency_key"),
    )
    publish_event("OrderReadyForPayment", event_data, source=ORDER_SOURCE, correlation_id=correlation_id)

    logger.info("Inventory reserved — awaiting payment", order_id=order_id, saga_state="PAYMENT_PROCESSING")
    return saga_state


def handle_inventory_failed(order_id, reason, correlation_id=None):
    logger.set_correlation_id(correlation_id)

    transition_saga_state(
        order_id=order_id, from_state="INVENTORY_RESERVING", to_state="INVENTORY_FAILED",
        reason=f"Inventory reservation failed: {reason}",
    )
    update_order_status(order_id, "CANCELLED", cancellation_reason=reason)
    saga_state = transition_saga_state(
        order_id=order_id, from_state="INVENTORY_FAILED", to_state="CANCELLED",
        reason="Order cancelled due to inventory failure",
    )

    order = get_order(order_id)
    event_data = build_order_cancelled_event(
        order_id=order_id, user_id=order["user_id"],
        reason=f"Inventory unavailable: {reason}",
    )
    publish_event("OrderCanceled", event_data, source=ORDER_SOURCE, correlation_id=correlation_id)

    logger.info("Inventory failed — order cancelled", order_id=order_id, reason=reason)
    return saga_state


def handle_payment_completed(order_id, payment_id, charge_id, amount, correlation_id=None):
    logger.set_correlation_id(correlation_id)

    saga_state = transition_saga_state(
        order_id=order_id, from_state="PAYMENT_PROCESSING", to_state="CONFIRMED",
        reason=f"Payment completed: {payment_id}", payment_id=payment_id, charge_id=charge_id,
    )
    update_order_status(order_id, "CONFIRMED", payment_id=payment_id, charge_id=charge_id)

    order = get_order(order_id)
    event_data = build_order_confirmed_event(
        order_id=order_id, user_id=order["user_id"], items=order["items"],
        total_amount=float(order["total_amount"]), shipping_address=order.get("shipping_address"),
    )
    publish_event("OrderConfirmed", event_data, source=ORDER_SOURCE, correlation_id=correlation_id)

    logger.info("Order confirmed — saga complete", order_id=order_id, payment_id=payment_id)
    return saga_state


def handle_payment_failed(order_id, reason, correlation_id=None):
    logger.set_correlation_id(correlation_id)

    saga_state = transition_saga_state(
        order_id=order_id, from_state="PAYMENT_PROCESSING", to_state="PAYMENT_FAILED",
        reason=f"Payment failed: {reason}",
    )

    logger.warn("Payment failed — starting compensation", order_id=order_id, reason=reason)
    compensation.compensate_inventory(
        order_id=order_id, saga_state=saga_state, reason=reason, correlation_id=correlation_id,
    )
    return saga_state
