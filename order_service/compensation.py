"""
Compensation handlers for saga rollback.

When a saga step fails after previous steps have already succeeded,
compensation handlers undo the completed work in reverse order.

Currently there's one compensation scenario in M2:
    - Payment fails after inventory was reserved -> release the inventory.

The compensation flow:
    1. Order service detects payment failure
    2. Publishes saga.compensate_inventory event
    3. M4 inventory service releases the reserved stock
    4. M4 publishes inventory.released event
    5. Order service receives it, marks order as CANCELLED

If the compensation itself fails (e.g., inventory service is down), the saga
enters a COMPENSATING state. A dead-letter queue (DLQ) and CloudWatch
alarm (set up by M1) will catch these for manual resolution.
"""

from shared.events import (
    build_order_cancelled_event,
    build_saga_compensate_inventory_event,
    publish_event,
)
from shared.logger import get_logger

from .models import get_saga_state, transition_saga_state, update_order_status

logger = get_logger("order-service-compensation")


def compensate_inventory(
    order_id: str,
    saga_state: dict,
    reason: str,
    correlation_id: str = None,
) -> dict:
    """Request an inventory release as compensation for a failed payment.

    Does NOT release inventory directly -- instead, publishes a
    saga.compensate_inventory event that M4's inventory service listens for.
    This keeps services decoupled: the order service doesn't need to know
    how inventory release works internally.

    Args:
        order_id: The order to compensate.
        saga_state: The current saga state (contains reservation_id).
        reason: Why compensation is needed (e.g., "Card declined").
        correlation_id: Trace ID.

    Returns:
        The updated saga state.
    """
    logger.set_correlation_id(correlation_id)

    # Transition: PAYMENT_FAILED -> COMPENSATING
    saga_state = transition_saga_state(
        order_id=order_id,
        from_state="PAYMENT_FAILED",
        to_state="COMPENSATING",
        reason=f"Starting compensation: release inventory. Cause: {reason}",
    )

    # Update order status to reflect compensation is in progress
    update_order_status(order_id, "COMPENSATING")

    # Publish event for M4 inventory service to release the reserved stock
    reservation_id = saga_state.get("reservation_id", "")

    event_data = build_saga_compensate_inventory_event(
        order_id=order_id,
        reservation_id=reservation_id,
        reason=reason,
    )
    publish_event(
        "saga.compensate_inventory", event_data, correlation_id=correlation_id
    )

    logger.info(
        "Compensation event published -- awaiting inventory release confirmation",
        order_id=order_id,
        reservation_id=reservation_id,
    )

    return saga_state


def handle_inventory_released(
    order_id: str, correlation_id: str = None
) -> dict:
    """Handle the inventory.released event -- finalize the cancellation.

    Called when M4's inventory service confirms the reserved stock was released.
    This is the final step of the compensation flow.

    Args:
        order_id: The order whose inventory was released.
        correlation_id: Trace ID.

    Returns:
        The updated saga state.
    """
    logger.set_correlation_id(correlation_id)

    # Transition: COMPENSATING -> CANCELLED
    saga_state = transition_saga_state(
        order_id=order_id,
        from_state="COMPENSATING",
        to_state="CANCELLED",
        reason="Inventory released. Order cancelled.",
    )

    # Update order to final CANCELLED state
    update_order_status(
        order_id,
        "CANCELLED",
        cancellation_reason="Compensation completed -- inventory released",
    )

    # Publish order.cancelled for M4 (notification service sends cancellation email)
    from .models import get_order

    order = get_order(order_id)
    event_data = build_order_cancelled_event(
        order_id=order_id,
        user_id=order["user_id"] if order else "unknown",
        reason="Order cancelled after compensation -- inventory released",
    )
    publish_event("order.cancelled", event_data, correlation_id=correlation_id)

    logger.info(
        "Compensation complete -- order cancelled and inventory released",
        order_id=order_id,
        saga_state="CANCELLED",
    )

    return saga_state
