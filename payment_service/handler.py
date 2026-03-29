"""
Lambda handler for the payment service.

This service is entirely event-driven -- it has no API Gateway routes.
All triggers come from EventBridge events published by the order service.

Events consumed:
  1. order.ready_for_payment -> charge the customer's card
  2. saga.compensate_payment -> refund a previous charge (compensation)

Events published:
  1. payment.completed -> tells order service the charge succeeded
  2. payment.failed -> tells order service the charge was declined
  3. payment.refunded -> tells order service the refund was processed

The handler extracts event data, applies idempotency checks, calls Stripe
via the circuit-breaker-protected client, and publishes result events.
"""

import json
import traceback

from shared.events import (
    build_payment_completed_event,
    build_payment_failed_event,
    publish_event,
)
from shared.exceptions import (
    CircuitBreakerOpenError,
    DuplicatePaymentError,
    PaymentFailedError,
)
from shared.logger import get_logger

from .idempotency import process_with_idempotency
from .models import create_payment, get_payment_by_order, update_payment_refund
from .stripe_client import create_charge, create_refund

logger = get_logger("payment-service")


def event_handler(event, context):
    """Lambda entry point for EventBridge events.

    Routes events by detail-type to the appropriate handler function.

    Args:
        event: EventBridge event with detail-type and detail fields.
        context: Lambda context (provides aws_request_id as fallback correlation ID).
    """
    detail_type = event.get("detail-type", "")
    detail = event.get("detail", {})

    # Parse the event envelope
    if isinstance(detail, str):
        detail = json.loads(detail)

    metadata = detail.get("metadata", {})
    data = detail.get("data", {})
    correlation_id = metadata.get("correlation_id", context.aws_request_id)

    logger.set_correlation_id(correlation_id)
    logger.info(
        "Event received",
        detail_type=detail_type,
        order_id=data.get("order_id"),
    )

    try:
        # --- Event: order.ready_for_payment -> process payment ---
        if detail_type == "order.ready_for_payment":
            _handle_ready_for_payment(data, correlation_id)

        # --- Event: saga.compensate_payment -> issue refund ---
        elif detail_type == "saga.compensate_payment":
            _handle_compensate_payment(data, correlation_id)

        else:
            logger.warn("Unknown event type -- ignoring", detail_type=detail_type)

    except Exception as e:
        logger.error(
            "Error processing event",
            detail_type=detail_type,
            order_id=data.get("order_id"),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        # Re-raise so Lambda marks invocation as failed -> EventBridge retries or DLQ
        raise


def _handle_ready_for_payment(data: dict, correlation_id: str):
    """Process an order.ready_for_payment event by charging the customer.

    This event is published by the order service after inventory has been
    successfully reserved by M4.

    Flow:
    1. Check idempotency key -- if already processed, skip.
    2. Call Stripe to create a charge.
    3. Store the payment record in DynamoDB.
    4. Publish payment.completed or payment.failed event.

    Args:
        data: Event payload (order_id, user_id, items, total_amount, etc.).
        correlation_id: Trace ID for log correlation.
    """
    order_id = data["order_id"]
    amount = float(data["total_amount"])
    currency = data.get("currency", "USD")
    idempotency_key = data.get("idempotency_key")

    logger.info(
        "Processing payment for order",
        order_id=order_id,
        amount=amount,
        currency=currency,
    )

    try:
        # Use idempotency wrapper to prevent duplicate charges.
        # The inner function (charge_and_record) only runs if the idempotency
        # key hasn't been seen before.
        def charge_and_record():
            """Inner function: charge Stripe and record the payment."""
            # Call Stripe to create the charge
            charge_result = create_charge(
                amount=amount,
                currency=currency,
                order_id=order_id,
                idempotency_key=idempotency_key,
            )

            # Store the payment record in DynamoDB
            payment = create_payment(
                order_id=order_id,
                amount=amount,
                currency=currency,
                charge_id=charge_result["charge_id"],
                idempotency_key=idempotency_key,
            )

            return payment

        # Execute with idempotency protection
        payment = process_with_idempotency(
            idempotency_key=idempotency_key,
            process_fn=charge_and_record,
        )

        # Publish payment.completed event -> order service confirms the order
        event_data = build_payment_completed_event(
            order_id=order_id,
            payment_id=payment["payment_id"],
            charge_id=payment["charge_id"],
            amount=amount,
            currency=currency,
        )
        publish_event("payment.completed", event_data, correlation_id=correlation_id)

        logger.info(
            "Payment completed successfully",
            order_id=order_id,
            payment_id=payment["payment_id"],
            charge_id=payment["charge_id"],
        )

    except PaymentFailedError as e:
        # Card declined or payment error -- publish payment.failed
        logger.warn("Payment failed", order_id=order_id, reason=e.message)

        event_data = build_payment_failed_event(
            order_id=order_id,
            reason=e.message,
            error_code=e.error_code,
        )
        publish_event("payment.failed", event_data, correlation_id=correlation_id)

    except CircuitBreakerOpenError as e:
        # Stripe is down -- publish payment.failed so the saga can handle it.
        # The DLQ will also capture this for retry when Stripe recovers.
        logger.error("Circuit breaker open -- Stripe unavailable", order_id=order_id)

        event_data = build_payment_failed_event(
            order_id=order_id,
            reason="Payment provider temporarily unavailable",
            error_code="CIRCUIT_BREAKER_OPEN",
        )
        publish_event("payment.failed", event_data, correlation_id=correlation_id)


def _handle_compensate_payment(data: dict, correlation_id: str):
    """Process a saga.compensate_payment event by refunding the charge.

    Called when the order service needs to undo a successful payment
    (e.g., post-confirmation cancellation in future extensions).

    Flow:
    1. Look up the payment record for the order.
    2. Call Stripe to create a refund.
    3. Update the payment record with refund info.
    4. Publish payment.refunded event.

    Args:
        data: Event payload (order_id, payment_id, charge_id, amount, reason).
        correlation_id: Trace ID.
    """
    order_id = data["order_id"]
    charge_id = data.get("charge_id")
    reason = data.get("reason", "Saga compensation")

    logger.info(
        "Processing compensation refund",
        order_id=order_id,
        charge_id=charge_id,
        reason=reason,
    )

    # Look up the payment record -- we need the payment_id and charge_id
    payment = get_payment_by_order(order_id)
    if not payment:
        logger.error("No payment found for order -- cannot refund", order_id=order_id)
        return

    # Use the charge_id from the payment record (more reliable than event data)
    actual_charge_id = payment.get("charge_id", charge_id)
    payment_amount = float(payment.get("amount", 0))

    try:
        # Call Stripe to create the refund
        refund_result = create_refund(
            charge_id=actual_charge_id,
            amount=payment_amount,
            reason="requested_by_customer",
            order_id=order_id,
        )

        # Update the payment record with refund info
        update_payment_refund(
            payment_id=payment["payment_id"],
            refund_id=refund_result["refund_id"],
            refund_amount=payment_amount,
        )

        # Publish payment.refunded -> order service finalizes the cancellation
        publish_event(
            "payment.refunded",
            {
                "order_id": order_id,
                "payment_id": payment["payment_id"],
                "refund_id": refund_result["refund_id"],
                "amount": payment_amount,
            },
            correlation_id=correlation_id,
        )

        logger.info(
            "Refund completed",
            order_id=order_id,
            refund_id=refund_result["refund_id"],
            amount=payment_amount,
        )

    except Exception as e:
        # Refund failed -- log it. The DLQ will capture this event for retry.
        logger.error(
            "Refund failed",
            order_id=order_id,
            charge_id=actual_charge_id,
            error=str(e),
            traceback=traceback.format_exc(),
        )
        raise
