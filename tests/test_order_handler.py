"""
Tests for the order service API handler.

These tests verify:
  - POST /orders creates an order and starts the saga.
  - GET /orders/{id} returns the order.
  - PUT /orders/{id}/cancel cancels a pending order.
  - Input validation rejects malformed requests.
  - Error handling returns proper status codes.
"""

import json

import pytest


class TestCreateOrder:
    """Tests for POST /orders endpoint."""

    def test_create_order_success(self, aws_mock, sample_order_items, lambda_context, mocker):
        """Creating an order should return 201 with the order and start the saga."""
        # Mock the EventBridge publish so we don't need a real bus
        mocker.patch("shared.events.publish_event", return_value={"FailedEntryCount": 0})

        from order_service.handler import api_handler

        event = {
            "httpMethod": "POST",
            "path": "/orders",
            "headers": {"X-Correlation-Id": "test-corr-123"},
            "body": json.dumps({
                "user_id": "usr_test001",
                "items": sample_order_items,
                "shipping_address": {
                    "street": "123 Main St",
                    "city": "Boston",
                    "state": "MA",
                    "zip": "02101",
                },
            }),
            "pathParameters": None,
        }

        response = api_handler(event, lambda_context)

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert "order" in body
        assert body["order"]["status"] == "PENDING"
        assert body["order"]["user_id"] == "usr_test001"
        assert body["order"]["order_id"].startswith("ord_")

    def test_create_order_missing_user_id(self, aws_mock, lambda_context):
        """Should return 400 when user_id is missing."""
        from order_service.handler import api_handler

        event = {
            "httpMethod": "POST",
            "path": "/orders",
            "headers": {},
            "body": json.dumps({"items": [{"product_id": "p1", "quantity": 1, "unit_price": 10}]}),
            "pathParameters": None,
        }

        response = api_handler(event, lambda_context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error_code"] == "VALIDATION_ERROR"

    def test_create_order_missing_items(self, aws_mock, lambda_context):
        """Should return 400 when items array is missing."""
        from order_service.handler import api_handler

        event = {
            "httpMethod": "POST",
            "path": "/orders",
            "headers": {},
            "body": json.dumps({"user_id": "usr_test001"}),
            "pathParameters": None,
        }

        response = api_handler(event, lambda_context)

        assert response["statusCode"] == 400

    def test_create_order_invalid_item_fields(self, aws_mock, lambda_context):
        """Should return 400 when items are missing required fields."""
        from order_service.handler import api_handler

        event = {
            "httpMethod": "POST",
            "path": "/orders",
            "headers": {},
            "body": json.dumps({
                "user_id": "usr_test001",
                # Missing unit_price in item
                "items": [{"product_id": "p1", "quantity": 1}],
            }),
            "pathParameters": None,
        }

        response = api_handler(event, lambda_context)

        assert response["statusCode"] == 400


class TestGetOrder:
    """Tests for GET /orders/{id} endpoint."""

    def test_get_order_success(self, aws_mock, sample_order_items, lambda_context, mocker):
        """Should return the order when it exists."""
        mocker.patch("shared.events.publish_event", return_value={"FailedEntryCount": 0})

        from order_service.handler import api_handler
        from order_service.models import create_order

        # Create an order first
        order = create_order(user_id="usr_test001", items=sample_order_items)

        event = {
            "httpMethod": "GET",
            "path": f"/orders/{order['order_id']}",
            "headers": {},
            "pathParameters": {"id": order["order_id"]},
            "body": None,
        }

        response = api_handler(event, lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["order"]["order_id"] == order["order_id"]

    def test_get_order_not_found(self, aws_mock, lambda_context):
        """Should return 404 when the order doesn't exist."""
        from order_service.handler import api_handler

        event = {
            "httpMethod": "GET",
            "path": "/orders/ord_nonexistent",
            "headers": {},
            "pathParameters": {"id": "ord_nonexistent"},
            "body": None,
        }

        response = api_handler(event, lambda_context)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error_code"] == "ORDER_NOT_FOUND"


class TestCancelOrder:
    """Tests for PUT /orders/{id}/cancel endpoint."""

    def test_cancel_pending_order(self, aws_mock, sample_order_items, lambda_context, mocker):
        """Cancelling a PENDING order should succeed immediately (no refund needed)."""
        mocker.patch("shared.events.publish_event", return_value={"FailedEntryCount": 0})

        from order_service.handler import api_handler
        from order_service.models import create_order

        order = create_order(user_id="usr_test001", items=sample_order_items)

        event = {
            "httpMethod": "PUT",
            "path": f"/orders/{order['order_id']}/cancel",
            "headers": {},
            "pathParameters": {"id": order["order_id"]},
            "body": None,
        }

        response = api_handler(event, lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["message"] == "Order cancelled"

    def test_cancel_nonexistent_order(self, aws_mock, lambda_context):
        """Cancelling a non-existent order should return 404."""
        from order_service.handler import api_handler

        event = {
            "httpMethod": "PUT",
            "path": "/orders/ord_nonexistent/cancel",
            "headers": {},
            "pathParameters": {"id": "ord_nonexistent"},
            "body": None,
        }

        response = api_handler(event, lambda_context)

        assert response["statusCode"] == 404


class TestRouting:
    """Tests for API route matching."""

    def test_unknown_route_returns_404(self, aws_mock, lambda_context):
        """Unknown routes should return 404."""
        from order_service.handler import api_handler

        event = {
            "httpMethod": "DELETE",
            "path": "/orders/ord_123",
            "headers": {},
            "pathParameters": {"id": "ord_123"},
            "body": None,
        }

        response = api_handler(event, lambda_context)

        assert response["statusCode"] == 404
