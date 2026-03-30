"""
DynamoDB models for the payment service.

Two tables:
  1. PaymentsTable — stores payment records (charge ID, amount, status, refund info).
  2. IdempotencyKeysTable — prevents duplicate charges when events are retried.
     Each key maps to a cached response, so retried requests return the same result.

Design decisions:
  - Payments are keyed by payment_id (auto-generated UUID).
  - Payments also have a GSI on order_id so we can look up payments for an order.
  - Idempotency keys have a 24-hour TTL — after that, DynamoDB auto-deletes them.
    This is safe because EventBridge retries happen within minutes, not days.
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3

# Table names injected via SAM template environment variables
PAYMENTS_TABLE = os.environ.get("PAYMENTS_TABLE", "PaymentsTable")
IDEMPOTENCY_TABLE = os.environ.get("IDEMPOTENCY_TABLE", "IdempotencyKeysTable")

# Lazy-initialized DynamoDB resource
_dynamodb = None


def _get_table(table_name: str):
    """Get a DynamoDB Table resource, lazily initializing the connection."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb.Table(table_name)


# ---------------------------------------------------------------------------
# Payment CRUD
# ---------------------------------------------------------------------------


def create_payment(
    order_id: str,
    amount: float,
    currency: str,
    charge_id: str,
    idempotency_key: str,
) -> dict:
    """Create a new payment record after a successful Stripe charge.

    Args:
        order_id: The order this payment is for.
        amount: The charged amount.
        currency: ISO 4217 currency code (e.g., "USD").
        charge_id: The Stripe charge ID (e.g., "ch_abc123").
        idempotency_key: The key used to prevent duplicate charges.

    Returns:
        The complete payment record as stored in DynamoDB.
    """
    payment_id = f"pay_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()

    payment = {
        "payment_id": payment_id,
        "order_id": order_id,
        "amount": str(amount),  # DynamoDB decimal handling
        "currency": currency,
        "charge_id": charge_id,
        "idempotency_key": idempotency_key,
        "status": "COMPLETED",
        "refund_id": None,
        "refund_amount": None,
        "created_at": now,
        "updated_at": now,
    }

    table = _get_table(PAYMENTS_TABLE)
    table.put_item(Item=payment)

    return payment


def get_payment(payment_id: str) -> Optional[dict]:
    """Fetch a payment by its ID."""
    table = _get_table(PAYMENTS_TABLE)
    response = table.get_item(Key={"payment_id": payment_id})
    return response.get("Item")


def get_payment_by_order(order_id: str) -> Optional[dict]:
    """Fetch the payment for a given order using the GSI.

    Returns the first (and typically only) payment for the order.
    """
    table = _get_table(PAYMENTS_TABLE)
    response = table.query(
        IndexName="order_id-index",
        KeyConditionExpression="order_id = :oid",
        ExpressionAttributeValues={":oid": order_id},
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None


def update_payment_refund(payment_id: str, refund_id: str, refund_amount: float) -> dict:
    """Record a refund against an existing payment.

    Args:
        payment_id: The payment to refund.
        refund_id: The Stripe refund ID (e.g., "re_abc123").
        refund_amount: The amount refunded.

    Returns:
        The updated payment record.
    """
    table = _get_table(PAYMENTS_TABLE)
    now = datetime.now(timezone.utc).isoformat()

    response = table.update_item(
        Key={"payment_id": payment_id},
        UpdateExpression=(
            "SET #status = :status, refund_id = :refund_id, "
            "refund_amount = :refund_amount, updated_at = :now"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "REFUNDED",
            ":refund_id": refund_id,
            ":refund_amount": str(refund_amount),
            ":now": now,
        },
        ReturnValues="ALL_NEW",
    )

    return response["Attributes"]


# ---------------------------------------------------------------------------
# Idempotency Key Management
# ---------------------------------------------------------------------------

# TTL: 24 hours in seconds. DynamoDB automatically deletes expired items.
IDEMPOTENCY_TTL_SECONDS = 86400


def check_idempotency_key(idempotency_key: str) -> Optional[dict]:
    """Check if an idempotency key has already been used.

    If the key exists, we've already processed this payment request.
    Return the cached result instead of charging again.

    Args:
        idempotency_key: The client-provided deduplication key.

    Returns:
        The cached payment result dict, or None if key is new.
    """
    table = _get_table(IDEMPOTENCY_TABLE)
    response = table.get_item(Key={"idempotency_key": idempotency_key})
    item = response.get("Item")

    if item:
        return item.get("cached_result")
    return None


def store_idempotency_key(idempotency_key: str, result: dict) -> None:
    """Store an idempotency key with its cached result.

    Future requests with the same key will get this cached result
    instead of being processed again.

    Args:
        idempotency_key: The deduplication key.
        result: The payment result to cache (payment record dict).
    """
    import time

    table = _get_table(IDEMPOTENCY_TABLE)
    now = datetime.now(timezone.utc).isoformat()

    table.put_item(
        Item={
            "idempotency_key": idempotency_key,
            "cached_result": result,
            "created_at": now,
            # TTL attribute — DynamoDB auto-deletes this item after expiry
            "ttl": int(time.time()) + IDEMPOTENCY_TTL_SECONDS,
        }
    )
