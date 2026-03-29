"""
Tests for the payment service event handler.

These tests verify:
  - order.ready_for_payment event triggers a Stripe charge and publishes payment.completed.
  - Idempotent retries return the cached result without double-charging.
  - Payment failures publish payment.failed events.
  - saga.compensate_payment triggers a Stripe refund.
"""

import json

import pytest


class TestOrderReadyForPaymentEvent:
    """Tests for handling order.ready_for_payment events (charge the card)."""

    def test_successful_payment(self, aws_mock, lambda_context, mocker):
        """A valid order.ready_for_payment event should charge Stripe and publish payment.completed."""
        mock_charge = mocker.patch(
            "payment_service.stripe_client.stripe.Charge.create",
            return_value=mocker.Mock(id="ch_test_123", status="succeeded"),
        )
        mock_publish = mocker.patch(
            "payment_service.handler.publish_event",
            return_value={"FailedEntryCount": 0},
        )

        from payment_service.handler import event_handler

        event = {
            "detail-type": "order.ready_for_payment",
            "detail": {
                "metadata": {"correlation_id": "corr-123"},
                "data": {
                    "order_id": "ord_test001",
                    "user_id": "usr_test001",
                    "items": [{"product_id": "p1", "quantity": 1, "unit_price": 29.99}],
                    "total_amount": 29.99,
                    "currency": "USD",
                    "idempotency_key": "idem_test001",
                },
            },
        }

        event_handler(event, lambda_context)

        # Verify Stripe was called
        mock_charge.assert_called_once()
        charge_kwargs = mock_charge.call_args
        assert charge_kwargs[1]["amount"] == 2999  # $29.99 in cents

        # Verify payment.completed event was published
        mock_publish.assert_called()
        publish_calls = mock_publish.call_args_list
        completed_call = [c for c in publish_calls if c[0][0] == "payment.completed"]
        assert len(completed_call) == 1

    def test_idempotent_retry(self, aws_mock, lambda_context, mocker):
        """Retrying the same event should return the cached result without charging again."""
        mock_charge = mocker.patch(
            "payment_service.stripe_client.stripe.Charge.create",
            return_value=mocker.Mock(id="ch_test_456", status="succeeded"),
        )
        mocker.patch(
            "shared.events.publish_event",
            return_value={"FailedEntryCount": 0},
        )

        from payment_service.handler import event_handler

        event = {
            "detail-type": "order.ready_for_payment",
            "detail": {
                "metadata": {"correlation_id": "corr-456"},
                "data": {
                    "order_id": "ord_test002",
                    "user_id": "usr_test002",
                    "items": [{"product_id": "p1", "quantity": 1, "unit_price": 15.00}],
                    "total_amount": 15.00,
                    "currency": "USD",
                    "idempotency_key": "idem_test002",
                },
            },
        }

        # First call -- should charge Stripe
        event_handler(event, lambda_context)
        assert mock_charge.call_count == 1

        # Second call with same idempotency key -- should NOT charge again
        event_handler(event, lambda_context)
        assert mock_charge.call_count == 1  # Still 1 -- no second charge

    def test_payment_card_declined(self, aws_mock, lambda_context, mocker):
        """A declined card should publish payment.failed."""
        import stripe

        mocker.patch(
            "payment_service.stripe_client.stripe.Charge.create",
            side_effect=stripe.CardError(
                message="Your card was declined",
                param="number",
                code="card_declined",
            ),
        )
        mock_publish = mocker.patch(
            "payment_service.handler.publish_event",
            return_value={"FailedEntryCount": 0},
        )

        from payment_service.handler import event_handler

        event = {
            "detail-type": "order.ready_for_payment",
            "detail": {
                "metadata": {"correlation_id": "corr-789"},
                "data": {
                    "order_id": "ord_test003",
                    "user_id": "usr_test003",
                    "items": [{"product_id": "p1", "quantity": 1, "unit_price": 99.99}],
                    "total_amount": 99.99,
                    "currency": "USD",
                    "idempotency_key": "idem_test003",
                },
            },
        }

        event_handler(event, lambda_context)

        # Verify payment.failed event was published
        publish_calls = mock_publish.call_args_list
        failed_calls = [c for c in publish_calls if c[0][0] == "payment.failed"]
        assert len(failed_calls) == 1


class TestCompensatePaymentEvent:
    """Tests for handling saga.compensate_payment events (refund)."""

    def test_successful_refund(self, aws_mock, lambda_context, mocker):
        """A compensate_payment event should refund via Stripe and publish payment.refunded."""
        mocker.patch(
            "payment_service.stripe_client.stripe.Charge.create",
            return_value=mocker.Mock(id="ch_refund_test", status="succeeded"),
        )
        mock_refund = mocker.patch(
            "payment_service.stripe_client.stripe.Refund.create",
            return_value=mocker.Mock(id="re_test_123", amount=2999, status="succeeded"),
        )
        mock_publish = mocker.patch(
            "payment_service.handler.publish_event",
            return_value={"FailedEntryCount": 0},
        )

        from payment_service.handler import event_handler

        # Step 1: Create the payment by handling order.ready_for_payment
        create_event = {
            "detail-type": "order.ready_for_payment",
            "detail": {
                "metadata": {"correlation_id": "corr-refund"},
                "data": {
                    "order_id": "ord_refund001",
                    "user_id": "usr_refund001",
                    "items": [{"product_id": "p1", "quantity": 1, "unit_price": 29.99}],
                    "total_amount": 29.99,
                    "currency": "USD",
                    "idempotency_key": "idem_refund001",
                },
            },
        }
        event_handler(create_event, lambda_context)

        # Step 2: Request compensation (refund)
        compensate_event = {
            "detail-type": "saga.compensate_payment",
            "detail": {
                "metadata": {"correlation_id": "corr-refund"},
                "data": {
                    "order_id": "ord_refund001",
                    "payment_id": "pay_test",
                    "charge_id": "ch_refund_test",
                    "amount": 29.99,
                    "reason": "Post-confirmation cancellation",
                },
            },
        }
        event_handler(compensate_event, lambda_context)

        # Verify Stripe refund was called
        mock_refund.assert_called_once()

        # Verify payment.refunded event was published
        publish_calls = mock_publish.call_args_list
        refunded_calls = [c for c in publish_calls if c[0][0] == "payment.refunded"]
        assert len(refunded_calls) == 1
