"""
Idempotency layer for payment processing.

Problem: EventBridge guarantees at-least-once delivery, which means the payment
service might receive the same order.created event multiple times. Without
idempotency, we'd charge the customer's card twice.

Solution: Before processing a charge, we check the idempotency key table.
  - If the key exists → return the cached result (no new charge).
  - If the key is new → process the charge, then store the result.

This provides exactly-once semantics at the application level:
  - First attempt: charge card, store result with idempotency key.
  - Retry attempts: find existing key, return cached result.

We also pass the idempotency key to Stripe's API (Stripe has its own built-in
idempotency), so we have double protection:
  1. Our DynamoDB table catches retries before they even hit Stripe.
  2. Stripe's idempotency catches any edge cases we miss.
"""

from shared.logger import get_logger

from .models import check_idempotency_key, store_idempotency_key

logger = get_logger("payment-idempotency")


def process_with_idempotency(idempotency_key: str, process_fn, *args, **kwargs) -> dict:
    """Execute a payment operation with idempotency protection.

    Checks if the operation was already performed. If so, returns the cached
    result. If not, executes the operation and caches the result.

    Args:
        idempotency_key: Unique key for this operation (generated when the order
                         is created — see order_service/models.py).
        process_fn: The function to execute if the operation is new.
                    Must return a dict (the payment result).
        *args, **kwargs: Arguments to pass to process_fn.

    Returns:
        The payment result dict — either freshly computed or from cache.

    Example:
        result = process_with_idempotency(
            idempotency_key="idem_abc123",
            process_fn=charge_customer,
            order_id="ord_001",
            amount=59.99,
        )
    """
    # Step 1: Check if we've already processed this payment
    cached_result = check_idempotency_key(idempotency_key)
    if cached_result:
        logger.info(
            "Idempotency key found — returning cached result (no duplicate charge)",
            idempotency_key=idempotency_key,
            cached_payment_id=cached_result.get("payment_id"),
        )
        return cached_result

    # Step 2: Key is new — process the payment
    logger.info(
        "New idempotency key — processing payment",
        idempotency_key=idempotency_key,
    )
    result = process_fn(*args, **kwargs)

    # Step 3: Cache the result so retries don't re-process
    store_idempotency_key(idempotency_key, result)
    logger.info(
        "Payment result cached for idempotency key",
        idempotency_key=idempotency_key,
        payment_id=result.get("payment_id"),
    )

    return result
