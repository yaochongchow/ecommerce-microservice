"""
Stripe SDK wrapper with retry logic and circuit breaker.

This module wraps the Stripe Python SDK to provide:
  1. Automatic retries with exponential backoff — handles transient network errors.
  2. Circuit breaker — stops hammering Stripe when it's consistently failing,
     failing fast instead of waiting for timeouts.

Circuit Breaker States:
  - CLOSED (normal): Requests go through. If failures exceed the threshold,
    transitions to OPEN.
  - OPEN (rejecting): All requests fail immediately with CircuitBreakerOpenError.
    After the cooldown period, transitions to HALF_OPEN.
  - HALF_OPEN (testing): Allows one request through. If it succeeds, transitions
    back to CLOSED. If it fails, transitions back to OPEN.

Why a circuit breaker for Stripe?
  If Stripe is down or rate-limiting us, retrying every request adds load to both
  Stripe and our system. The circuit breaker "trips" after consecutive failures,
  giving Stripe time to recover while our Lambda functions fail fast (cheaper
  than waiting for timeouts).
"""

import os
import time
import uuid
from enum import Enum

from shared.exceptions import CircuitBreakerOpenError, PaymentFailedError, RefundFailedError
from shared.logger import get_logger

logger = get_logger("stripe-client")

# Payment mode: "mock" auto-approves without calling Stripe, "live" uses real Stripe
PAYMENT_MODE = os.environ.get("PAYMENT_MODE", "mock")

if PAYMENT_MODE != "mock":
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "sk_test_placeholder")


# ---------------------------------------------------------------------------
# Circuit Breaker Implementation
# ---------------------------------------------------------------------------


class CircuitState(Enum):
    """The three states of a circuit breaker."""
    CLOSED = "CLOSED"        # Normal operation — requests flow through
    OPEN = "OPEN"            # Tripped — all requests rejected immediately
    HALF_OPEN = "HALF_OPEN"  # Testing — one request allowed to check recovery


class CircuitBreaker:
    """Simple in-memory circuit breaker for external API calls.

    Note: This is per-Lambda-instance. Each Lambda container has its own
    circuit breaker state. This is fine for our use case — if Stripe is down,
    each container will independently trip after a few failures.

    For shared state across all containers, you'd use DynamoDB or ElastiCache,
    but that adds latency to every call and isn't worth it here.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 30,
        service_name: str = "stripe",
    ):
        """
        Args:
            failure_threshold: Number of consecutive failures before tripping.
            recovery_timeout: Seconds to wait in OPEN state before trying again.
            service_name: Name for logging purposes.
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.service_name = service_name

        # Current state
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0

    def can_execute(self) -> bool:
        """Check if a request is allowed to go through.

        Returns:
            True if the request should proceed, False if it should be rejected.
        """
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if the cooldown period has elapsed
            elapsed = time.time() - self.last_failure_time
            if elapsed >= self.recovery_timeout:
                # Transition to HALF_OPEN — allow one test request
                self.state = CircuitState.HALF_OPEN
                logger.info(
                    "Circuit breaker transitioning to HALF_OPEN",
                    service=self.service_name,
                    elapsed_seconds=elapsed,
                )
                return True
            return False

        # HALF_OPEN: allow the test request
        return True

    def record_success(self):
        """Record a successful call. Resets the circuit breaker to CLOSED."""
        if self.state == CircuitState.HALF_OPEN:
            logger.info("Circuit breaker closing — test request succeeded", service=self.service_name)

        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self):
        """Record a failed call. May trip the circuit breaker to OPEN."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warn(
                "Circuit breaker OPEN — too many failures",
                service=self.service_name,
                failure_count=self.failure_count,
                recovery_timeout=self.recovery_timeout,
            )
        elif self.state == CircuitState.HALF_OPEN:
            # Test request failed — go back to OPEN
            self.state = CircuitState.OPEN
            logger.warn(
                "Circuit breaker re-opened — half-open test failed",
                service=self.service_name,
            )


# Global circuit breaker instance (per Lambda container)
_circuit_breaker = CircuitBreaker()


# ---------------------------------------------------------------------------
# Retry Logic
# ---------------------------------------------------------------------------

# Retry configuration
MAX_RETRIES = 3
BASE_DELAY = 0.5  # seconds — doubles each retry (0.5s, 1s, 2s)


def _retry_with_backoff(operation, *args, **kwargs):
    """Execute an operation with exponential backoff retries.

    Only retries on transient errors (network issues, rate limits).
    Does not retry on permanent errors (invalid card, authentication failure).

    Args:
        operation: The function to call (e.g., stripe.Charge.create).
        *args, **kwargs: Arguments to pass to the function.

    Returns:
        The result of the operation.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exception = None

    for attempt in range(MAX_RETRIES + 1):
        # Check circuit breaker before each attempt
        if not _circuit_breaker.can_execute():
            raise CircuitBreakerOpenError("stripe")

        try:
            result = operation(*args, **kwargs)
            _circuit_breaker.record_success()
            return result

        except stripe.RateLimitError as e:
            # Stripe is rate-limiting us — retry with backoff
            last_exception = e
            _circuit_breaker.record_failure()
            logger.warn(
                "Rate limited by Stripe — retrying",
                attempt=attempt + 1,
                max_retries=MAX_RETRIES,
            )

        except stripe.APIConnectionError as e:
            # Network issue — retry with backoff
            last_exception = e
            _circuit_breaker.record_failure()
            logger.warn(
                "Stripe connection error — retrying",
                attempt=attempt + 1,
                error=str(e),
            )

        except stripe.CardError as e:
            # Card declined — this is a permanent error, don't retry
            _circuit_breaker.record_success()  # Stripe responded, so it's healthy
            raise PaymentFailedError(
                order_id=kwargs.get("metadata", {}).get("order_id", "unknown"),
                reason=str(e.user_message),
            )

        except stripe.AuthenticationError as e:
            # Bad API key — permanent error, don't retry
            logger.error("Stripe authentication failed — check API key", error=str(e))
            raise

        # Exponential backoff: 0.5s, 1s, 2s
        if attempt < MAX_RETRIES:
            delay = BASE_DELAY * (2 ** attempt)
            logger.info("Retrying after backoff", delay_seconds=delay)
            time.sleep(delay)

    # All retries exhausted
    _circuit_breaker.record_failure()
    raise last_exception


# ---------------------------------------------------------------------------
# Public API — Charge and Refund
# ---------------------------------------------------------------------------


def create_charge(
    amount: float,
    currency: str,
    source: str = "tok_visa",
    order_id: str = None,
    idempotency_key: str = None,
) -> dict:
    if PAYMENT_MODE == "mock":
        charge_id = f"ch_mock_{uuid.uuid4().hex[:12]}"
        logger.info("Mock charge created", charge_id=charge_id, amount=amount, order_id=order_id)
        return {"charge_id": charge_id, "amount": amount, "currency": currency, "status": "succeeded"}

    """Create a Stripe charge with retry and circuit breaker protection.

    Args:
        amount: Amount to charge (in dollars — Stripe wants cents, we convert).
        currency: ISO 4217 currency code.
        source: Stripe token or source ID. Defaults to test token.
        order_id: Order ID to attach as metadata (for Stripe Dashboard lookups).
        idempotency_key: Stripe's native idempotency key — Stripe itself deduplicates
                         requests with the same key within 24 hours.

    Returns:
        Dict with charge_id, amount, currency, and status.

    Raises:
        PaymentFailedError: If the card is declined.
        CircuitBreakerOpenError: If Stripe is consistently failing.
    """
    logger.info(
        "Creating Stripe charge",
        amount=amount,
        currency=currency,
        order_id=order_id,
    )

    # Stripe expects amounts in cents (smallest currency unit)
    amount_cents = int(amount * 100)

    charge = _retry_with_backoff(
        stripe.Charge.create,
        amount=amount_cents,
        currency=currency.lower(),
        source=source,
        metadata={"order_id": order_id or ""},
        idempotency_key=idempotency_key,
    )

    logger.info(
        "Stripe charge created",
        charge_id=charge.id,
        order_id=order_id,
    )

    return {
        "charge_id": charge.id,
        "amount": amount,
        "currency": currency,
        "status": charge.status,
    }


def create_refund(
    charge_id: str,
    amount: float = None,
    reason: str = "requested_by_customer",
    order_id: str = None,
) -> dict:
    if PAYMENT_MODE == "mock":
        refund_id = f"re_mock_{uuid.uuid4().hex[:12]}"
        logger.info("Mock refund created", refund_id=refund_id, charge_id=charge_id, order_id=order_id)
        return {"refund_id": refund_id, "charge_id": charge_id, "amount": amount or 0, "status": "succeeded"}

    """Create a Stripe refund with retry and circuit breaker protection.

    Args:
        charge_id: The Stripe charge ID to refund (e.g., "ch_abc123").
        amount: Amount to refund in dollars. If None, refunds the full charge.
        reason: Stripe refund reason. One of: duplicate, fraudulent,
                requested_by_customer.
        order_id: Order ID for logging.

    Returns:
        Dict with refund_id, charge_id, amount, and status.

    Raises:
        RefundFailedError: If the refund cannot be processed.
        CircuitBreakerOpenError: If Stripe is consistently failing.
    """
    logger.info(
        "Creating Stripe refund",
        charge_id=charge_id,
        amount=amount,
        order_id=order_id,
    )

    refund_params = {
        "charge": charge_id,
        "reason": reason,
        "metadata": {"order_id": order_id or ""},
    }

    # If amount is specified, convert to cents for partial refund
    if amount is not None:
        refund_params["amount"] = int(amount * 100)

    try:
        refund = _retry_with_backoff(stripe.Refund.create, **refund_params)
    except Exception as e:
        raise RefundFailedError(
            order_id=order_id or "unknown",
            reason=str(e),
        )

    logger.info(
        "Stripe refund created",
        refund_id=refund.id,
        charge_id=charge_id,
        order_id=order_id,
    )

    return {
        "refund_id": refund.id,
        "charge_id": charge_id,
        "amount": amount or float(refund.amount) / 100,
        "status": refund.status,
    }
