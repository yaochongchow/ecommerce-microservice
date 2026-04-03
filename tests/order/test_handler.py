"""Tests for the order service API handler."""

import json
import pytest


class TestCreateOrder:
    def test_create_order_success(self, aws_mock, sample_order_items, lambda_context, mocker):
        mocker.patch("saga.publish_event", return_value={"FailedEntryCount": 0})
        from handler import api_handler

        event = {
            "httpMethod": "POST",
            "path": "/orders",
            "headers": {"X-Correlation-Id": "test-corr-123"},
            "body": json.dumps({
                "user_id": "usr_test001",
                "items": sample_order_items,
                "shipping_address": {"street": "123 Main St", "city": "Boston", "state": "MA", "zip": "02101"},
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
        from handler import api_handler
        event = {
            "httpMethod": "POST", "path": "/orders", "headers": {},
            "body": json.dumps({"items": [{"product_id": "p1", "quantity": 1, "unit_price": 10}]}),
            "pathParameters": None,
        }
        response = api_handler(event, lambda_context)
        assert response["statusCode"] == 400

    def test_create_order_missing_items(self, aws_mock, lambda_context):
        from handler import api_handler
        event = {
            "httpMethod": "POST", "path": "/orders", "headers": {},
            "body": json.dumps({"user_id": "usr_test001"}),
            "pathParameters": None,
        }
        response = api_handler(event, lambda_context)
        assert response["statusCode"] == 400

    def test_create_order_invalid_item_fields(self, aws_mock, lambda_context):
        from handler import api_handler
        event = {
            "httpMethod": "POST", "path": "/orders", "headers": {},
            "body": json.dumps({"user_id": "usr_test001", "items": [{"product_id": "p1", "quantity": 1}]}),
            "pathParameters": None,
        }
        response = api_handler(event, lambda_context)
        assert response["statusCode"] == 400


class TestGetOrder:
    def test_get_order_success(self, aws_mock, sample_order_items, lambda_context, mocker):
        mocker.patch("saga.publish_event", return_value={"FailedEntryCount": 0})
        from handler import api_handler
        from models import create_order

        order = create_order(user_id="usr_test001", items=sample_order_items)
        event = {
            "httpMethod": "GET", "path": f"/orders/{order['order_id']}",
            "headers": {}, "pathParameters": {"id": order["order_id"]}, "body": None,
        }

        response = api_handler(event, lambda_context)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["order"]["order_id"] == order["order_id"]

    def test_get_order_not_found(self, aws_mock, lambda_context):
        from handler import api_handler
        event = {
            "httpMethod": "GET", "path": "/orders/ord_nonexistent",
            "headers": {}, "pathParameters": {"id": "ord_nonexistent"}, "body": None,
        }
        response = api_handler(event, lambda_context)
        assert response["statusCode"] == 404


class TestCancelOrder:
    def test_cancel_pending_order(self, aws_mock, sample_order_items, lambda_context, mocker):
        mocker.patch("saga.publish_event", return_value={"FailedEntryCount": 0})
        from handler import api_handler
        from models import create_order

        order = create_order(user_id="usr_test001", items=sample_order_items)
        event = {
            "httpMethod": "PUT", "path": f"/orders/{order['order_id']}/cancel",
            "headers": {}, "pathParameters": {"id": order["order_id"]}, "body": None,
        }

        response = api_handler(event, lambda_context)
        assert response["statusCode"] == 200

    def test_cancel_nonexistent_order(self, aws_mock, lambda_context):
        from handler import api_handler
        event = {
            "httpMethod": "PUT", "path": "/orders/ord_nonexistent/cancel",
            "headers": {}, "pathParameters": {"id": "ord_nonexistent"}, "body": None,
        }
        response = api_handler(event, lambda_context)
        assert response["statusCode"] == 404


class TestRouting:
    def test_unknown_route_returns_404(self, aws_mock, lambda_context):
        from handler import api_handler
        event = {
            "httpMethod": "DELETE", "path": "/orders/ord_123",
            "headers": {}, "pathParameters": {"id": "ord_123"}, "body": None,
        }
        response = api_handler(event, lambda_context)
        assert response["statusCode"] == 404
