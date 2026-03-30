"""Tests for the payment service event handler."""

import json
import pytest


class TestOrderReadyForPaymentEvent:
    def test_successful_payment(self, aws_mock, lambda_context, mocker):
        mock_charge = mocker.patch(
            "payment.stripe_client.stripe.Charge.create",
            return_value=mocker.Mock(id="ch_test_123", status="succeeded"),
        )
        mock_publish = mocker.patch(
            "payment.handler.publish_event",
            return_value={"FailedEntryCount": 0},
        )
        from payment.handler import event_handler

        event = {
            "detail-type": "OrderReadyForPayment",
            "detail": {
                "orderId": "ord_test001",
                "userId": "usr_test001",
                "items": [{"productId": "p1", "quantity": 1, "unitPrice": 29.99}],
                "totalAmount": 29.99,
                "currency": "USD",
                "idempotencyKey": "idem_test001",
                "correlationId": "corr-123",
            },
        }

        event_handler(event, lambda_context)
        mock_charge.assert_called_once()
        assert mock_charge.call_args[1]["amount"] == 2999

        publish_calls = mock_publish.call_args_list
        completed_call = [c for c in publish_calls if c[0][0] == "PaymentSucceeded"]
        assert len(completed_call) == 1

    def test_idempotent_retry(self, aws_mock, lambda_context, mocker):
        mock_charge = mocker.patch(
            "payment.stripe_client.stripe.Charge.create",
            return_value=mocker.Mock(id="ch_test_456", status="succeeded"),
        )
        mocker.patch("shared.events.publish_event", return_value={"FailedEntryCount": 0})
        from payment.handler import event_handler

        event = {
            "detail-type": "OrderReadyForPayment",
            "detail": {
                "orderId": "ord_test002",
                "userId": "usr_test002",
                "items": [{"productId": "p1", "quantity": 1, "unitPrice": 15.00}],
                "totalAmount": 15.00,
                "currency": "USD",
                "idempotencyKey": "idem_test002",
                "correlationId": "corr-456",
            },
        }

        event_handler(event, lambda_context)
        assert mock_charge.call_count == 1
        event_handler(event, lambda_context)
        assert mock_charge.call_count == 1  # No duplicate charge

    def test_payment_card_declined(self, aws_mock, lambda_context, mocker):
        import stripe
        mocker.patch(
            "payment.stripe_client.stripe.Charge.create",
            side_effect=stripe.CardError(message="Declined", param="number", code="card_declined"),
        )
        mock_publish = mocker.patch(
            "payment.handler.publish_event",
            return_value={"FailedEntryCount": 0},
        )
        from payment.handler import event_handler

        event = {
            "detail-type": "OrderReadyForPayment",
            "detail": {
                "orderId": "ord_test003",
                "userId": "usr_test003",
                "items": [{"productId": "p1", "quantity": 1, "unitPrice": 99.99}],
                "totalAmount": 99.99,
                "currency": "USD",
                "idempotencyKey": "idem_test003",
                "correlationId": "corr-789",
            },
        }

        event_handler(event, lambda_context)
        failed_calls = [c for c in mock_publish.call_args_list if c[0][0] == "PaymentFailed"]
        assert len(failed_calls) == 1


class TestCompensatePaymentEvent:
    def test_successful_refund(self, aws_mock, lambda_context, mocker):
        mocker.patch(
            "payment.stripe_client.stripe.Charge.create",
            return_value=mocker.Mock(id="ch_refund_test", status="succeeded"),
        )
        mock_refund = mocker.patch(
            "payment.stripe_client.stripe.Refund.create",
            return_value=mocker.Mock(id="re_test_123", amount=2999, status="succeeded"),
        )
        mock_publish = mocker.patch(
            "payment.handler.publish_event",
            return_value={"FailedEntryCount": 0},
        )
        from payment.handler import event_handler

        # Step 1: Create payment
        create_event = {
            "detail-type": "OrderReadyForPayment",
            "detail": {
                "orderId": "ord_refund001",
                "userId": "usr_refund001",
                "items": [{"productId": "p1", "quantity": 1, "unitPrice": 29.99}],
                "totalAmount": 29.99,
                "currency": "USD",
                "idempotencyKey": "idem_refund001",
                "correlationId": "corr-refund",
            },
        }
        event_handler(create_event, lambda_context)

        # Step 2: Refund
        compensate_event = {
            "detail-type": "CompensatePayment",
            "detail": {
                "orderId": "ord_refund001",
                "chargeId": "ch_refund_test",
                "reason": "Post-confirmation cancellation",
                "correlationId": "corr-refund",
            },
        }
        event_handler(compensate_event, lambda_context)

        mock_refund.assert_called_once()
        refunded_calls = [c for c in mock_publish.call_args_list if c[0][0] == "PaymentRefunded"]
        assert len(refunded_calls) == 1
