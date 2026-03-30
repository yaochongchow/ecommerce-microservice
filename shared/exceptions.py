"""
Custom exception types for the order and payment microservices.

These exceptions provide structured error handling across the saga workflow.
Each exception carries an error_code used in API responses and event payloads
so downstream consumers can programmatically react to specific failure modes.
"""


class BaseServiceError(Exception):
    """Base exception for all service errors.

    Attributes:
        message: Human-readable error description.
        error_code: Machine-readable error code for API responses.
        status_code: HTTP status code to return (for API-triggered errors).
    """

    def __init__(self, message: str, error_code: str, status_code: int = 500):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code

    def to_dict(self) -> dict:
        """Serialize the error for API responses or event payloads."""
        return {
            "error_code": self.error_code,
            "message": self.message,
        }


# --- Order Service Exceptions ---


class OrderNotFoundError(BaseServiceError):
    """Raised when an order ID does not exist in the database."""

    def __init__(self, order_id: str):
        super().__init__(
            message=f"Order {order_id} not found",
            error_code="ORDER_NOT_FOUND",
            status_code=404,
        )


class OrderAlreadyCancelledError(BaseServiceError):
    """Raised when attempting to cancel an order that is already cancelled."""

    def __init__(self, order_id: str):
        super().__init__(
            message=f"Order {order_id} is already cancelled",
            error_code="ORDER_ALREADY_CANCELLED",
            status_code=409,
        )


class InvalidOrderStateError(BaseServiceError):
    """Raised when a saga transition is attempted from an invalid state.

    For example, trying to confirm an order that hasn't completed payment.
    """

    def __init__(self, order_id: str, current_state: str, attempted_action: str):
        super().__init__(
            message=f"Order {order_id} in state {current_state} cannot perform {attempted_action}",
            error_code="INVALID_ORDER_STATE",
            status_code=409,
        )


# --- Payment Service Exceptions ---


class PaymentFailedError(BaseServiceError):
    """Raised when a payment charge is declined or fails at the provider."""

    def __init__(self, order_id: str, reason: str):
        super().__init__(
            message=f"Payment failed for order {order_id}: {reason}",
            error_code="PAYMENT_FAILED",
            status_code=402,
        )


class RefundFailedError(BaseServiceError):
    """Raised when a refund cannot be processed (e.g., charge not found)."""

    def __init__(self, order_id: str, reason: str):
        super().__init__(
            message=f"Refund failed for order {order_id}: {reason}",
            error_code="REFUND_FAILED",
            status_code=500,
        )


class DuplicatePaymentError(BaseServiceError):
    """Raised when an idempotency key has already been used.

    This is not actually an error — it means we already processed this payment
    and should return the cached result instead of charging again.
    """

    def __init__(self, idempotency_key: str):
        super().__init__(
            message=f"Payment with idempotency key {idempotency_key} already processed",
            error_code="DUPLICATE_PAYMENT",
            status_code=409,
        )


# --- Saga Exceptions ---


class SagaCompensationError(BaseServiceError):
    """Raised when a compensation (rollback) step itself fails.

    This is a critical error — it means the system is in an inconsistent state
    and requires manual intervention or a retry mechanism.
    """

    def __init__(self, order_id: str, failed_step: str, reason: str):
        super().__init__(
            message=f"Compensation failed for order {order_id} at step {failed_step}: {reason}",
            error_code="SAGA_COMPENSATION_FAILED",
            status_code=500,
        )


# --- Infrastructure Exceptions ---


class CircuitBreakerOpenError(BaseServiceError):
    """Raised when the circuit breaker is open and calls are being rejected.

    The Stripe client uses a circuit breaker to avoid hammering a failing
    external service. When open, all calls fail fast until the cooldown expires.
    """

    def __init__(self, service_name: str):
        super().__init__(
            message=f"Circuit breaker open for {service_name} — calls are being rejected",
            error_code="CIRCUIT_BREAKER_OPEN",
            status_code=503,
        )
