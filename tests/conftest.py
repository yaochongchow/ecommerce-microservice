"""
Shared test fixtures for all service tests.

Uses moto to mock AWS services so tests run locally without real AWS credentials.
"""

import os
import sys

# Add service and layer paths so imports resolve correctly
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_project_root, "services"))
sys.path.insert(0, os.path.join(_project_root, "layers", "common", "python"))

import boto3
import pytest
from moto import mock_aws

# Set environment variables BEFORE importing application code
os.environ["ORDERS_TABLE"] = "test-orders"
os.environ["SAGA_STATE_TABLE"] = "test-saga-state"
os.environ["PAYMENTS_TABLE"] = "test-payments"
os.environ["IDEMPOTENCY_TABLE"] = "test-idempotency-keys"
os.environ["INVENTORY_TABLE_NAME"] = "test-inventory"
os.environ["RESERVATIONS_TABLE_NAME"] = "test-reservations"
os.environ["SHIPMENTS_TABLE_NAME"] = "test-shipments"
os.environ["EVENT_BUS_NAME"] = "test-event-bus"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
os.environ["EMAIL_MODE"] = "mock"
os.environ["LOW_STOCK_THRESHOLD"] = "10"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"


@pytest.fixture
def aws_mock():
    """Start moto mock and create all DynamoDB tables + EventBridge bus."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

        # Order service tables
        dynamodb.create_table(
            TableName="test-orders",
            KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="test-saga-state",
            KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # Payment service tables
        dynamodb.create_table(
            TableName="test-payments",
            KeySchema=[{"AttributeName": "payment_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "payment_id", "AttributeType": "S"},
                {"AttributeName": "order_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[{
                "IndexName": "order_id-index",
                "KeySchema": [{"AttributeName": "order_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )
        dynamodb.create_table(
            TableName="test-idempotency-keys",
            KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # Inventory service tables
        dynamodb.create_table(
            TableName="test-inventory",
            KeySchema=[{"AttributeName": "productId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "productId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="test-reservations",
            KeySchema=[
                {"AttributeName": "orderId", "KeyType": "HASH"},
                {"AttributeName": "productId", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "orderId", "AttributeType": "S"},
                {"AttributeName": "productId", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Shipping service table
        dynamodb.create_table(
            TableName="test-shipments",
            KeySchema=[{"AttributeName": "shipmentId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "shipmentId", "AttributeType": "S"},
                {"AttributeName": "orderId", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[{
                "IndexName": "orderId-index",
                "KeySchema": [{"AttributeName": "orderId", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )

        # EventBridge bus
        events_client = boto3.client("events", region_name="us-east-1")
        events_client.create_event_bus(Name="test-event-bus")

        # Reset lazy-initialized clients in modules
        import shared.events
        shared.events._eventbridge_client = None

        try:
            import order.models
            order.models._dynamodb = None
        except (ImportError, AttributeError):
            pass

        try:
            import payment.models
            payment.models._dynamodb = None
        except (ImportError, AttributeError):
            pass

        yield dynamodb


@pytest.fixture
def sample_order_items():
    """Sample order line items."""
    return [
        {"product_id": "prod_001", "quantity": 2, "unit_price": 29.99},
        {"product_id": "prod_002", "quantity": 1, "unit_price": 49.99},
    ]


@pytest.fixture
def lambda_context():
    """Minimal mock Lambda context."""
    class MockContext:
        aws_request_id = "test-request-id-12345"
        function_name = "test-function"
        memory_limit_in_mb = 256
        invoked_function_arn = "arn:aws:lambda:us-east-1:123456789:function:test"
    return MockContext()
