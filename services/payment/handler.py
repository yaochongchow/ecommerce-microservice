"""
Lambda handler for the payment service.

Events consumed:
  OrderReadyForPayment -> charge the customer
  CompensatePayment -> refund a previous charge

Events published:
  PaymentSucceeded -> order service confirms the order
  PaymentFailed -> order service starts compensation
"""

import json
import traceback

from common.event_utils import unwrap_event, get_detail_type, get_detail
from shared.events import build_payment_completed_event, build_payment_failed_event, publish_event
from shared.exceptions import CircuitBreakerOpenError, DuplicatePaymentError, PaymentFailedError
from shared.logger import get_logger

from idempotency import process_with_idempotency
from models import create_payment, get_payment_by_order, update_payment_refund
from stripe_client import create_charge, create_refund

logger = get_logger("payment-service")

PAYMENT_SOURCE = "payment-service"


def event_handler(event, context):
    eb_event = unwrap_event(event)
    detail_type = get_detail_type(eb_event)
    detail = get_detail(eb_event)

    correlation_id = detail.get("correlationId", context.aws_request_id)
    logger.set_correlation_id(correlation_id)
    logger.info("Event received", detail_type=detail_type, order_id=detail.get("orderId"))

    try:
        if detail_type == "OrderReadyForPayment":
            _handle_ready_for_payment(detail, correlation_id)
        elif detail_type == "CompensatePayment":
            _handle_compensate_payment(detail, correlation_id)
        else:
            logger.warn("Unknown event type", detail_type=detail_type)

    except Exception as e:
        logger.error("Error processing event", detail_type=detail_type, order_id=detail.get("orderId"), error=str(e), traceback=traceback.format_exc())
        raise


def _handle_ready_for_payment(data, correlation_id):
    order_id = data["orderId"]
    amount = float(data["totalAmount"])
    currency = data.get("currency", "USD")
    idempotency_key = data.get("idempotencyKey")

    logger.info("Processing payment", order_id=order_id, amount=amount)

    try:
        def charge_and_record():
            charge_result = create_charge(
                amount=amount, currency=currency,
                order_id=order_id, idempotency_key=idempotency_key,
            )
            payment = create_payment(
                order_id=order_id, amount=amount, currency=currency,
                charge_id=charge_result["charge_id"], idempotency_key=idempotency_key,
            )
            return payment

        payment = process_with_idempotency(idempotency_key=idempotency_key, process_fn=charge_and_record)

        event_data = build_payment_completed_event(
            order_id=order_id, payment_id=payment["payment_id"],
            charge_id=payment["charge_id"], amount=amount, currency=currency,
        )
        publish_event("PaymentSucceeded", event_data, source=PAYMENT_SOURCE, correlation_id=correlation_id)
        logger.info("Payment completed", order_id=order_id, payment_id=payment["payment_id"])

    except PaymentFailedError as e:
        logger.warn("Payment failed", order_id=order_id, reason=e.message)
        event_data = build_payment_failed_event(order_id=order_id, reason=e.message, error_code=e.error_code)
        publish_event("PaymentFailed", event_data, source=PAYMENT_SOURCE, correlation_id=correlation_id)

    except CircuitBreakerOpenError:
        logger.error("Circuit breaker open", order_id=order_id)
        event_data = build_payment_failed_event(order_id=order_id, reason="Payment provider temporarily unavailable", error_code="CIRCUIT_BREAKER_OPEN")
        publish_event("PaymentFailed", event_data, source=PAYMENT_SOURCE, correlation_id=correlation_id)


def _handle_compensate_payment(data, correlation_id):
    order_id = data["orderId"]
    charge_id = data.get("chargeId")
    reason = data.get("reason", "Saga compensation")

    logger.info("Processing refund", order_id=order_id, reason=reason)

    payment = get_payment_by_order(order_id)
    if not payment:
        logger.error("No payment found", order_id=order_id)
        return

    actual_charge_id = payment.get("charge_id", charge_id)
    payment_amount = float(payment.get("amount", 0))

    try:
        refund_result = create_refund(
            charge_id=actual_charge_id, amount=payment_amount,
            reason="requested_by_customer", order_id=order_id,
        )
        update_payment_refund(
            payment_id=payment["payment_id"],
            refund_id=refund_result["refund_id"],
            refund_amount=payment_amount,
        )
        publish_event(
            "PaymentRefunded",
            {"orderId": order_id, "paymentId": payment["payment_id"],
             "refundId": refund_result["refund_id"], "amount": payment_amount},
            source=PAYMENT_SOURCE, correlation_id=correlation_id,
        )
        logger.info("Refund completed", order_id=order_id, refund_id=refund_result["refund_id"])

    except Exception as e:
        logger.error("Refund failed", order_id=order_id, error=str(e), traceback=traceback.format_exc())
        raise
