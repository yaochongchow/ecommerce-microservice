"""
Compensation handlers for saga rollback.

When payment fails after inventory was reserved, compensation releases the stock.
"""

from shared.events import (
    build_order_cancelled_event,
    build_saga_compensate_inventory_event,
    publish_event,
)
from shared.logger import get_logger
from .models import get_saga_state, transition_saga_state, update_order_status

logger = get_logger("order-service-compensation")

ORDER_SOURCE = "order-service"


def compensate_inventory(order_id, saga_state, reason, correlation_id=None):
    logger.set_correlation_id(correlation_id)

    saga_state = transition_saga_state(
        order_id=order_id, from_state="PAYMENT_FAILED", to_state="COMPENSATING",
        reason=f"Starting compensation: release inventory. Cause: {reason}",
    )
    update_order_status(order_id, "COMPENSATING")

    reservation_id = saga_state.get("reservation_id", "")
    event_data = build_saga_compensate_inventory_event(
        order_id=order_id, reservation_id=reservation_id, reason=reason,
    )
    publish_event("CompensateInventory", event_data, source=ORDER_SOURCE, correlation_id=correlation_id)

    logger.info("Compensation event published", order_id=order_id, reservation_id=reservation_id)
    return saga_state


def handle_inventory_released(order_id, correlation_id=None):
    logger.set_correlation_id(correlation_id)

    saga_state = transition_saga_state(
        order_id=order_id, from_state="COMPENSATING", to_state="CANCELLED",
        reason="Inventory released. Order cancelled.",
    )
    update_order_status(order_id, "CANCELLED", cancellation_reason="Compensation completed — inventory released")

    from .models import get_order
    order = get_order(order_id)
    event_data = build_order_cancelled_event(
        order_id=order_id,
        user_id=order["user_id"] if order else "unknown",
        reason="Order cancelled after compensation — inventory released",
    )
    publish_event("OrderCanceled", event_data, source=ORDER_SOURCE, correlation_id=correlation_id)

    logger.info("Compensation complete — order cancelled", order_id=order_id)
    return saga_state
