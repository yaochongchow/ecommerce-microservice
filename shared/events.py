"""
EventBridge event publisher and schema definitions.

This module handles all communication between microservices via AWS EventBridge.
The order and payment services never call each other directly -- they publish events
to the shared EventBridge bus, and EventBridge rules route them to the right consumer.

Event flow for a successful order (inventory-first):
    1. order.created              -> M4 inventory service reserves stock
    2. inventory.reserved         -> Order service advances saga to payment step
    3. order.ready_for_payment    -> Payment service charges the card
    4. payment.completed          -> Order service confirms the order
    5. order.confirmed            -> M4 notification + shipping services

Event flow for inventory failure:
    1. order.created              -> M4 inventory service, stock unavailable
    2. inventory.failed           -> Order service cancels (no compensation needed)

Event flow for compensation (payment fails after inventory reserved):
    1. payment.failed             -> Order service detects failure
    2. saga.compensate_inventory  -> M4 inventory service releases reserved stock
    3. inventory.released         -> Order service finalizes cancellation
"""

import json
import os
from datetime import datetime, timezone
from typing import Any

import boto3

# The EventBridge bus name -- set by the SAM template as an environment variable.
# M1 creates this bus; M2 just publishes to it.
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "ecommerce-event-bus")

# Source identifier -- EventBridge uses this to route events via rules.
EVENT_SOURCE = "ecommerce.m2"

# Initialize the EventBridge client (reused across Lambda invocations for efficiency)
_eventbridge_client = None


def _get_client():
    """Lazy-initialize the EventBridge client.

    We use lazy init so the module can be imported in tests without
    needing real AWS credentials.
    """
    global _eventbridge_client
    if _eventbridge_client is None:
        _eventbridge_client = boto3.client("events")
    return _eventbridge_client


def publish_event(
    detail_type: str,
    detail: dict,
    correlation_id: str = None,
    source: str = EVENT_SOURCE,
) -> dict:
    """Publish an event to the EventBridge bus.

    Args:
        detail_type: The event type (e.g., "order.created", "payment.completed").
                     EventBridge rules match on this to route to consumers.
        detail: The event payload -- must be JSON-serializable.
        correlation_id: Request trace ID, propagated so all services can correlate logs.
        source: The event source identifier. Defaults to "ecommerce.m2".

    Returns:
        The EventBridge PutEvents response (contains FailedEntryCount for error checking).
    """
    # Wrap the business payload with metadata envelope
    event_payload = {
        "metadata": {
            "correlation_id": correlation_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
        },
        "data": detail,
    }

    response = _get_client().put_events(
        Entries=[
            {
                "Source": source,
                "DetailType": detail_type,
                "Detail": json.dumps(event_payload, default=str),
                "EventBusName": EVENT_BUS_NAME,
            }
        ]
    )

    return response


# ---------------------------------------------------------------------------
# Event schema definitions -- these document the exact shape of each event's
# "data" field. Used by both publisher and consumer to stay in sync.
# In production, you'd validate against these with Pact contract tests (M4).
# ---------------------------------------------------------------------------


def build_order_created_event(
    order_id: str,
    user_id: str,
    items: list[dict],
    total_amount: float,
    currency: str = "USD",
    idempotency_key: str = None,
) -> dict:
    """Build the payload for an order.created event.

    Published by: Order service (after creating a new order).
    Consumed by: M4 inventory service (to reserve stock for the order items).

    Args:
        order_id: Unique order identifier.
        user_id: The customer who placed the order.
        items: List of order line items, each with product_id, quantity, price.
        total_amount: Total charge amount.
        currency: ISO 4217 currency code.
        idempotency_key: Key to prevent duplicate processing on retries.
    """
    return {
        "order_id": order_id,
        "user_id": user_id,
        "items": items,
        "total_amount": total_amount,
        "currency": currency,
        "idempotency_key": idempotency_key,
    }


def build_order_ready_for_payment_event(
    order_id: str,
    user_id: str,
    items: list[dict],
    total_amount: float,
    currency: str = "USD",
    idempotency_key: str = None,
) -> dict:
    """Build the payload for an order.ready_for_payment event.

    Published by: Order service (after inventory is successfully reserved).
    Consumed by: Payment service (to initiate the Stripe charge).

    Args:
        order_id: Unique order identifier.
        user_id: The customer who placed the order.
        items: List of order line items.
        total_amount: Total charge amount.
        currency: ISO 4217 currency code.
        idempotency_key: Key to prevent duplicate payment charges on retries.
    """
    return {
        "order_id": order_id,
        "user_id": user_id,
        "items": items,
        "total_amount": total_amount,
        "currency": currency,
        "idempotency_key": idempotency_key,
    }


def build_payment_completed_event(
    order_id: str,
    payment_id: str,
    charge_id: str,
    amount: float,
    currency: str = "USD",
) -> dict:
    """Build the payload for a payment.completed event.

    Published by: Payment service (after successful Stripe charge).
    Consumed by: Order service (to confirm the order -- final saga step).
    """
    return {
        "order_id": order_id,
        "payment_id": payment_id,
        "charge_id": charge_id,
        "amount": amount,
        "currency": currency,
    }


def build_payment_failed_event(
    order_id: str,
    reason: str,
    error_code: str = None,
) -> dict:
    """Build the payload for a payment.failed event.

    Published by: Payment service (when Stripe charge is declined or errors).
    Consumed by: Order service (to trigger compensation -- release inventory).
    """
    return {
        "order_id": order_id,
        "reason": reason,
        "error_code": error_code,
    }


def build_saga_compensate_inventory_event(
    order_id: str,
    reservation_id: str,
    reason: str,
) -> dict:
    """Build the payload for a saga.compensate_inventory event.

    Published by: Order service (when payment fails after inventory was reserved).
    Consumed by: M4 inventory service (to release the reserved stock).
    """
    return {
        "order_id": order_id,
        "reservation_id": reservation_id,
        "reason": reason,
    }


def build_order_confirmed_event(
    order_id: str,
    user_id: str,
    items: list[dict],
    total_amount: float,
    shipping_address: dict = None,
) -> dict:
    """Build the payload for an order.confirmed event.

    Published by: Order service (after all saga steps complete successfully).
    Consumed by: Shipping service + Notification service (M4).
    """
    return {
        "order_id": order_id,
        "user_id": user_id,
        "items": items,
        "total_amount": total_amount,
        "shipping_address": shipping_address,
    }


def build_order_cancelled_event(
    order_id: str,
    user_id: str,
    reason: str,
) -> dict:
    """Build the payload for an order.cancelled event.

    Published by: Order service (after compensation completes or user cancels).
    Consumed by: Inventory service (to release reserved stock -- M4),
                 Notification service (to send cancellation email -- M4).
    """
    return {
        "order_id": order_id,
        "user_id": user_id,
        "reason": reason,
    }
