"""
DynamoDB models for the order service.

Two tables:
  1. OrdersTable — stores order records (items, totals, status, user info).
  2. SagaStateTable — tracks the saga's current step and history for each order.
     Kept separate from orders so the saga engine can update state independently
     without conflicting with order-level writes.

Both tables use order_id as the partition key (no sort key) since we always
access by order ID. No GSIs are needed — we don't query orders by user or date
in M2 (the BFF in M1 would handle that via a GSI if needed).
"""

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Attr


def _float_to_decimal(obj):
    """Recursively convert floats to Decimals for DynamoDB compatibility.

    DynamoDB does not support Python float types — it requires Decimal.
    This converts all floats in nested dicts/lists before put_item calls.
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _float_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_float_to_decimal(i) for i in obj]
    return obj

# Table names injected via SAM template environment variables
ORDERS_TABLE = os.environ.get("ORDERS_TABLE", "OrdersTable")
SAGA_STATE_TABLE = os.environ.get("SAGA_STATE_TABLE", "SagaStateTable")

# Lazy-initialized DynamoDB resource (reused across invocations)
_dynamodb = None


def _get_table(table_name: str):
    """Get a DynamoDB Table resource, lazily initializing the connection."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb.Table(table_name)


# ---------------------------------------------------------------------------
# Order CRUD
# ---------------------------------------------------------------------------


def create_order(user_id: str, items: list[dict], shipping_address: dict = None) -> dict:
    """Create a new order in PENDING state.

    This is the first step of the saga. The order starts as PENDING and
    the saga engine will advance it through payment → inventory → confirmed.

    Args:
        user_id: The customer placing the order.
        items: List of line items, each with product_id, quantity, unit_price.
        shipping_address: Optional shipping address dict.

    Returns:
        The complete order record as stored in DynamoDB.
    """
    order_id = f"ord_{uuid.uuid4().hex[:12]}"
    idempotency_key = f"idem_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc).isoformat()

    # Calculate the total from line items
    total_amount = sum(
        item["quantity"] * item["unit_price"] for item in items
    )

    order = {
        "order_id": order_id,
        "user_id": user_id,
        "items": _float_to_decimal(items),  # Convert floats to Decimal for DynamoDB
        "total_amount": Decimal(str(total_amount)),
        "currency": "USD",
        "status": "PENDING",
        "shipping_address": shipping_address or {},
        "idempotency_key": idempotency_key,
        "created_at": now,
        "updated_at": now,
    }

    table = _get_table(ORDERS_TABLE)
    table.put_item(Item=order)

    return order


def get_order(order_id: str) -> Optional[dict]:
    """Fetch a single order by ID.

    Returns:
        The order dict, or None if not found.
    """
    table = _get_table(ORDERS_TABLE)
    response = table.get_item(Key={"order_id": order_id})
    return response.get("Item")


def list_orders(limit: int = 100) -> list[dict]:
    """Scan all orders, sorted by most recent first.

    Returns:
        List of order records (capped at limit).
    """
    table = _get_table(ORDERS_TABLE)
    items = []
    last_key = None
    while True:
        kwargs = {"Limit": limit}
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key or len(items) >= limit:
            break
    items.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    return items[:limit]


def update_order_status(order_id: str, new_status: str, **extra_fields) -> dict:
    """Update an order's status and optionally set additional fields.

    Uses a condition expression to prevent overwriting a CANCELLED order —
    once cancelled, an order cannot be moved to any other state.

    Args:
        order_id: The order to update.
        new_status: The new status string (e.g., "CONFIRMED", "CANCELLED").
        **extra_fields: Additional fields to set (e.g., payment_id, charge_id).

    Returns:
        The updated order record.
    """
    table = _get_table(ORDERS_TABLE)
    now = datetime.now(timezone.utc).isoformat()

    # Build the update expression dynamically
    update_parts = ["#status = :new_status", "updated_at = :now"]
    attr_names = {"#status": "status"}
    attr_values = {":new_status": new_status, ":now": now}

    for key, value in extra_fields.items():
        placeholder = f":{key}"
        update_parts.append(f"{key} = {placeholder}")
        attr_values[placeholder] = value

    update_expression = "SET " + ", ".join(update_parts)

    response = table.update_item(
        Key={"order_id": order_id},
        UpdateExpression=update_expression,
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=attr_values,
        # Prevent updating a cancelled order
        ConditionExpression=Attr("status").ne("CANCELLED"),
        ReturnValues="ALL_NEW",
    )

    return response["Attributes"]


# ---------------------------------------------------------------------------
# Saga State Management
# ---------------------------------------------------------------------------

# All possible saga states, in order. Used by the saga engine to determine
# what step to execute next and what to compensate on failure.
SAGA_STATES = [
    "PENDING",              # Order just created, nothing processed yet
    "INVENTORY_RESERVING",  # Waiting for inventory.reserved or inventory.failed
    "INVENTORY_RESERVED",   # Inventory reserved, about to request payment
    "PAYMENT_PROCESSING",   # Waiting for payment.completed or payment.failed
    "CONFIRMED",            # All steps succeeded -- order is finalized
    # Failure / compensation states:
    "INVENTORY_FAILED",     # Inventory unavailable -- cancel, no compensation needed
    "PAYMENT_FAILED",       # Payment declined after inventory reserved -- must release stock
    "COMPENSATING",         # Running compensation steps (release inventory)
    "CANCELLED",            # Order fully rolled back
]


def create_saga_state(order_id: str) -> dict:
    """Initialize the saga state for a new order.

    Called immediately after creating the order. The saga starts in PENDING
    and the history log tracks every state transition for debugging.

    Returns:
        The initial saga state record.
    """
    now = datetime.now(timezone.utc).isoformat()

    saga_state = {
        "order_id": order_id,
        "current_state": "PENDING",
        # History is an append-only log of all state transitions
        "history": [
            {
                "from_state": None,
                "to_state": "PENDING",
                "timestamp": now,
                "reason": "Order created",
            }
        ],
        # Store IDs needed for compensation (populated as saga progresses)
        "payment_id": None,
        "charge_id": None,
        "reservation_id": None,
        "created_at": now,
        "updated_at": now,
    }

    table = _get_table(SAGA_STATE_TABLE)
    table.put_item(Item=saga_state)

    return saga_state


def get_saga_state(order_id: str) -> Optional[dict]:
    """Fetch the current saga state for an order."""
    table = _get_table(SAGA_STATE_TABLE)
    response = table.get_item(Key={"order_id": order_id})
    return response.get("Item")


def transition_saga_state(
    order_id: str,
    from_state: str,
    to_state: str,
    reason: str,
    **extra_fields,
) -> dict:
    """Atomically transition the saga from one state to another.

    Uses a DynamoDB condition expression to ensure the saga is in the expected
    state before transitioning. This prevents race conditions — if two events
    arrive simultaneously, only one transition will succeed.

    Args:
        order_id: The order whose saga to transition.
        from_state: The expected current state (acts as optimistic lock).
        to_state: The target state.
        reason: Human-readable reason for the transition (logged in history).
        **extra_fields: Additional fields to update (e.g., payment_id, charge_id).

    Returns:
        The updated saga state record.

    Raises:
        ClientError: If the condition check fails (saga not in expected state).
    """
    table = _get_table(SAGA_STATE_TABLE)
    now = datetime.now(timezone.utc).isoformat()

    history_entry = {
        "from_state": from_state,
        "to_state": to_state,
        "timestamp": now,
        "reason": reason,
    }

    # Build update expression
    update_parts = [
        "current_state = :to_state",
        "history = list_append(history, :new_history)",
        "updated_at = :now",
    ]
    attr_values = {
        ":to_state": to_state,
        ":from_state": from_state,
        ":new_history": [history_entry],
        ":now": now,
    }

    for key, value in extra_fields.items():
        placeholder = f":{key}"
        update_parts.append(f"{key} = {placeholder}")
        attr_values[placeholder] = value

    update_expression = "SET " + ", ".join(update_parts)

    response = table.update_item(
        Key={"order_id": order_id},
        UpdateExpression=update_expression,
        ExpressionAttributeValues=attr_values,
        # Optimistic lock: only transition if currently in expected state
        ConditionExpression="current_state = :from_state",
        ReturnValues="ALL_NEW",
    )

    return response["Attributes"]
