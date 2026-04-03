"""Tests for the notification service."""

import importlib

import pytest


class TestOrderConfirmation:
    def test_order_confirmation_email(self, mocker):
        """PaymentSucceeded event should send an order confirmation email."""
        import email_client; importlib.reload(email_client)
        import service; importlib.reload(service)

        mock_send = mocker.patch("service.send_email")
        from service import notify_payment_succeeded

        detail = {
            "orderId": "ord_notify001",
            "userId": "usr_001",
            "email": "alice@test.com",
            "items": [{"productId": "p1", "quantity": 2}],
            "totalAmount": 89.99,
        }
        notify_payment_succeeded(detail)

        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        # Subject should be "Order Confirmation"
        assert call_kwargs[1]["subject"] == "Order Confirmation"
        # Email should be sent to the correct address
        assert call_kwargs[1]["to"] == "alice@test.com"
        # Body should contain the order ID
        assert "ord_notify001" in call_kwargs[1]["body"]

    def test_order_confirmation_includes_items(self, mocker):
        """Confirmation email body should list each item."""
        import email_client; importlib.reload(email_client)
        import service; importlib.reload(service)

        mock_send = mocker.patch("service.send_email")
        from service import notify_payment_succeeded

        detail = {
            "orderId": "ord_items001",
            "email": "alice@test.com",
            "items": [
                {"productId": "p1", "productName": "Widget", "quantity": 2},
                {"productId": "p2", "productName": "Gadget", "quantity": 1},
            ],
        }
        notify_payment_succeeded(detail)

        body = mock_send.call_args[1]["body"]
        assert "Widget" in body
        assert "Gadget" in body


class TestShipmentNotification:
    def test_shipment_email(self, mocker):
        """ShipmentCreated event should send a shipment tracking email."""
        import email_client; importlib.reload(email_client)
        import service; importlib.reload(service)

        mock_send = mocker.patch("service.send_email")
        from service import notify_shipment_created

        detail = {
            "orderId": "ord_ship_notify",
            "email": "bob@test.com",
            "carrier": "UPS_MOCK",
            "trackingNumber": "MOCK-20260329-A1B2",
            "shipmentId": "shp_001",
        }
        notify_shipment_created(detail)

        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert call_kwargs[1]["to"] == "bob@test.com"
        assert call_kwargs[1]["subject"] == "Your Order Has Shipped"
        assert "MOCK-20260329-A1B2" in call_kwargs[1]["body"]
        assert "UPS_MOCK" in call_kwargs[1]["body"]

    def test_shipment_email_contains_order_id(self, mocker):
        """Shipment email body should reference the order ID."""
        import email_client; importlib.reload(email_client)
        import service; importlib.reload(service)

        mock_send = mocker.patch("service.send_email")
        from service import notify_shipment_created

        detail = {
            "orderId": "ord_ship_ref",
            "email": "carol@test.com",
            "carrier": "UPS_MOCK",
            "trackingNumber": "MOCK-20260330-X9Z8",
            "shipmentId": "shp_002",
        }
        notify_shipment_created(detail)

        body = mock_send.call_args[1]["body"]
        assert "ord_ship_ref" in body


class TestCancellationNotification:
    def test_cancellation_email(self, mocker):
        """OrderCanceled event should send a cancellation email."""
        import email_client; importlib.reload(email_client)
        import service; importlib.reload(service)

        mock_send = mocker.patch("service.send_email")
        from service import notify_order_canceled

        detail = {
            "orderId": "ord_cancel_notify",
            "userId": "usr_001",
            "email": "dave@test.com",
            "reason": "Insufficient stock",
        }
        notify_order_canceled(detail)

        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert call_kwargs[1]["to"] == "dave@test.com"
        assert call_kwargs[1]["subject"] == "Your Order Has Been Cancelled"
        assert "Insufficient stock" in call_kwargs[1]["body"]
        assert "ord_cancel_notify" in call_kwargs[1]["body"]

    def test_cancellation_default_reason(self, mocker):
        """Cancellation with no explicit reason should use default text."""
        import email_client; importlib.reload(email_client)
        import service; importlib.reload(service)

        mock_send = mocker.patch("service.send_email")
        from service import notify_order_canceled

        detail = {
            "orderId": "ord_cancel_default",
            "email": "eve@test.com",
        }
        notify_order_canceled(detail)

        body = mock_send.call_args[1]["body"]
        assert "your order has been cancelled" in body


class TestEmailClient:
    def test_mock_mode_logs_instead_of_sending(self, mocker):
        """In mock mode (default in tests), send_email should log instead of calling SES."""
        import email_client; importlib.reload(email_client)

        mock_logger = mocker.patch("email_client.logger")
        from email_client import send_email

        send_email(to="test@test.com", subject="Test Subject", body="Hello World")

        # Should have logged the email details (mock mode)
        assert mock_logger.info.called
        log_messages = [str(c) for c in mock_logger.info.call_args_list]
        combined = " ".join(log_messages)
        assert "test@test.com" in combined
        assert "Test Subject" in combined

    def test_mock_mode_does_not_call_ses(self, mocker):
        """In mock mode, the SES client should never be invoked."""
        import email_client; importlib.reload(email_client)

        mock_ses = mocker.patch("email_client.ses_client")
        from email_client import send_email

        send_email(to="test@test.com", subject="Test", body="Body")

        mock_ses.send_email.assert_not_called()
