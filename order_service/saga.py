"""
Saga orchestrator for the order fulfillment workflow.

The saga pattern manages a distributed transaction across multiple services
(order -> inventory -> payment) without a shared database or two-phase commit.
Instead, each step publishes an event and waits for a response event. If any
step fails, compensation handlers run in reverse to undo completed steps.

Saga flow (happy path):
    PENDING -> INVENTORY_RESERVING -> INVENTORY_RESERVED -> PAYMENT_PROCESSING -> CONFIRMED

Saga flow (inventory fails):
    PENDING -> INVENTORY_RESERVING -> INVENTORY_FAILED -> CANCELLED
    (no compensation needed -- nothing to undo)

Saga flow (payment fails after inventory reserved):
    PENDING -> INVENTORY_RESERVING -> INVENTORY_RESERVED -> PAYMENT_PROCESSING
        -> PAYMENT_FAILED -> COMPENSATING -> CANCELLED
    (compensation: release the inventory reservation)

This module is the "brain" -- it decides what to do at each state transition.
The handler.py module calls these functions when events arrive.
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
from .models import (
    get_order,
    get_saga_state,
    transition_saga_state,
    update_order_status,
)

logger = get_logger("order-service-saga")


def start_saga(order: dict, correlation_id: str = None) -> dict:
    """Kick off the saga by transitioning to INVENTORY_RESERVING and publishing order.created.

    Called right after an order is created. This tells M4's inventory service
    to reserve stock for the order items.

    Args:
        order: The order record from DynamoDB.
        correlation_id: Trace ID for log correlation across services.

    Returns:
        The updated saga state.
    """
    logger.set_correlation_id(correlation_id)
    order_id = order["order_id"]

    # Transition saga: PENDING -> INVENTORY_RESERVING
    saga_state = transition_saga_state(
        order_id=order_id,
        from_state="PENDING",
        to_state="INVENTORY_RESERVING",
        reason="Initiating inventory reservation",
    )

    # Publish order.created event -- M4 inventory service will pick this up
    event_data = build_order_created_event(
        order_id=order_id,
        user_id=order["user_id"],
        items=order["items"],
        total_amount=float(order["total_amount"]),
        currency=order.get("currency", "USD"),
        idempotency_key=order.get("idempotency_key"),
    )
    publish_event("order.created", event_data, correlation_id=correlation_id)

    logger.info(
        "Saga started -- awaiting inventory reservation",
        order_id=order_id,
        saga_state="INVENTORY_RESERVING",
    )

    return saga_state


def handle_inventory_reserved(
    order_id: str, reservation_id: str, correlation_id: str = None
) -> dict:
    """Handle successful inventory reservation -- advance saga to payment.

    Called when the inventory.reserved event arrives from M4's inventory service.
    Stores the reservation ID in the saga state (needed for release if payment
    fails later), then triggers payment processing.

    Args:
        order_id: The order whose inventory was reserved.
        reservation_id: The inventory reservation ID from M4.
        correlation_id: Trace ID.

    Returns:
        The updated saga state.
    """
    logger.set_correlation_id(correlation_id)

    # Transition: INVENTORY_RESERVING -> INVENTORY_RESERVED
    saga_state = transition_saga_state(
        order_id=order_id,
        from_state="INVENTORY_RESERVING",
        to_state="INVENTORY_RESERVED",
        reason=f"Inventory reserved: {reservation_id}",
        reservation_id=reservation_id,
    )

    # Update the order record with reservation info
    update_order_status(order_id, "INVENTORY_RESERVED", reservation_id=reservation_id)

    # Now transition to PAYMENT_PROCESSING -- this tells the payment service
    # to charge the customer's card.
    saga_state = transition_saga_state(
        order_id=order_id,
        from_state="INVENTORY_RESERVED",
        to_state="PAYMENT_PROCESSING",
        reason="Inventory reserved -- requesting payment",
    )

    # Publish event for the payment service
    order = get_order(order_id)
    event_data = build_order_ready_for_payment_event(
        order_id=order_id,
        user_id=order["user_id"],
        items=order["items"],
        total_amount=float(order["total_amount"]),
        currency=order.get("currency", "USD"),
        idempotency_key=order.get("idempotency_key"),
    )
    publish_event("order.ready_for_payment", event_data, correlation_id=correlation_id)

    logger.info(
        "Inventory reserved -- awaiting payment",
        order_id=order_id,
        reservation_id=reservation_id,
        saga_state="PAYMENT_PROCESSING",
    )

    return saga_state


def handle_inventory_failed(
    order_id: str, reason: str, correlation_id: str = None
) -> dict:
    """Handle failed inventory reservation -- cancel the order (no compensation needed).

    When inventory fails, nothing else has happened yet (no payment charged),
    so we just cancel the order directly.

    Args:
        order_id: The order whose inventory reservation failed.
        reason: Why inventory failed (e.g., "Insufficient stock for SKU-123").
        correlation_id: Trace ID.

    Returns:
        The updated saga state.
    """
    logger.set_correlation_id(correlation_id)

    # Transition: INVENTORY_RESERVING -> INVENTORY_FAILED
    saga_state = transition_saga_state(
        order_id=order_id,
        from_state="INVENTORY_RESERVING",
        to_state="INVENTORY_FAILED",
        reason=f"Inventory reservation failed: {reason}",
    )

    # Cancel the order -- no compensation needed since nothing succeeded
    update_order_status(order_id, "CANCELLED", cancellation_reason=reason)

    saga_state = transition_saga_state(
        order_id=order_id,
        from_state="INVENTORY_FAILED",
        to_state="CANCELLED",
        reason="Order cancelled due to inventory failure",
    )

    # Publish order.cancelled so M4 notification service can email the customer
    order = get_order(order_id)
    event_data = build_order_cancelled_event(
        order_id=order_id,
        user_id=order["user_id"],
        reason=f"Inventory unavailable: {reason}",
    )
    publish_event("order.cancelled", event_data, correlation_id=correlation_id)

    logger.info(
        "Inventory failed -- order cancelled",
        order_id=order_id,
        reason=reason,
        saga_state="CANCELLED",
    )

    return saga_state


def handle_payment_completed(
    order_id: str,
    payment_id: str,
    charge_id: str,
    amount: float,
    correlation_id: str = None,
) -> dict:
    """Handle a successful payment -- confirm the order.

    This is the final happy-path step. All saga steps have succeeded:
    inventory reserved + payment charged = order confirmed.

    Args:
        order_id: The order that was paid for.
        payment_id: The payment record ID from the payment service.
        charge_id: The Stripe charge ID.
        amount: The amount charged.
        correlation_id: Trace ID.

    Returns:
        The updated saga state.
    """
    logger.set_correlation_id(correlation_id)

    # Transition: PAYMENT_PROCESSING -> CONFIRMED
    saga_state = transition_saga_state(
        order_id=order_id,
        from_state="PAYMENT_PROCESSING",
        to_state="CONFIRMED",
        reason=f"Payment completed: {payment_id}",
        payment_id=payment_id,
        charge_id=charge_id,
    )

    # Update the order to CONFIRMED
    update_order_status(
        order_id,
        "CONFIRMED",
        payment_id=payment_id,
        charge_id=charge_id,
    )

    # Publish order.confirmed -- M4 shipping + notification services consume this
    order = get_order(order_id)
    event_data = build_order_confirmed_event(
        order_id=order_id,
        user_id=order["user_id"],
        items=order["items"],
        total_amount=float(order["total_amount"]),
        shipping_address=order.get("shipping_address"),
    )
    publish_event("order.confirmed", event_data, correlation_id=correlation_id)

    logger.info(
        "Order confirmed -- saga complete",
        order_id=order_id,
        payment_id=payment_id,
        saga_state="CONFIRMED",
    )

    return saga_state


def handle_payment_failed(
    order_id: str, reason: str, correlation_id: str = None
) -> dict:
    """Handle a failed payment -- must compensate (release inventory).

    This is the key compensation scenario: inventory was reserved but payment
    failed. We need to release the reserved stock and cancel the order.

    Args:
        order_id: The order whose payment failed.
        reason: Why the payment failed (e.g., "Card declined").
        correlation_id: Trace ID.

    Returns:
        The updated saga state.
    """
    logger.set_correlation_id(correlation_id)

    # Transition: PAYMENT_PROCESSING -> PAYMENT_FAILED
    saga_state = transition_saga_state(
        order_id=order_id,
        from_state="PAYMENT_PROCESSING",
        to_state="PAYMENT_FAILED",
        reason=f"Payment failed: {reason}",
    )

    logger.warn(
        "Payment failed -- starting compensation (release inventory)",
        order_id=order_id,
        reason=reason,
    )

    # Run compensation: release the inventory reservation
    compensation.compensate_inventory(
        order_id=order_id,
        saga_state=saga_state,
        reason=reason,
        correlation_id=correlation_id,
    )

    return saga_state
