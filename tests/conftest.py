"""
Shared test fixtures for order and payment service tests.

Uses moto to mock AWS services (DynamoDB, EventBridge) so tests run locally
without needing real AWS credentials or infrastructure. Each test gets
fresh, isolated tables that are torn down after the test completes.
"""

import os

import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Set environment variables BEFORE importing application code.
# Our modules read these at import time to configure table names, etc.
# ---------------------------------------------------------------------------
os.environ["ORDERS_TABLE"] = "test-orders"
os.environ["SAGA_STATE_TABLE"] = "test-saga-state"
os.environ["PAYMENTS_TABLE"] = "test-payments"
os.environ["IDEMPOTENCY_TABLE"] = "test-idempotency-keys"
os.environ["EVENT_BUS_NAME"] = "test-event-bus"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"


@pytest.fixture
def aws_mock():
    """Start moto's AWS mock and create all DynamoDB tables needed by tests.

    This fixture:
    1. Activates moto's mock_aws context (intercepts all boto3 calls).
    2. Creates the 4 DynamoDB tables with the same schema as template.yaml.
    3. Creates the EventBridge event bus.
    4. Resets the lazy-initialized boto3 clients in our modules so they
       use the mocked AWS, not real AWS.
    5. Yields control to the test.
    6. Tears everything down after the test.
    """
    with mock_aws():
        # Create DynamoDB tables
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

        # Orders table
        dynamodb.create_table(
            TableName="test-orders",
            KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # Saga state table
        dynamodb.create_table(
            TableName="test-saga-state",
            KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # Payments table (with GSI on order_id)
        dynamodb.create_table(
            TableName="test-payments",
            KeySchema=[{"AttributeName": "payment_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "payment_id", "AttributeType": "S"},
                {"AttributeName": "order_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "order_id-index",
                    "KeySchema": [{"AttributeName": "order_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )

        # Idempotency keys table
        dynamodb.create_table(
            TableName="test-idempotency-keys",
            KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # Create EventBridge bus
        events_client = boto3.client("events", region_name="us-east-1")
        events_client.create_event_bus(Name="test-event-bus")

        # Reset lazy-initialized clients in our modules so they use the mock
        import shared.events
        import order_service.models
        import payment_service.models

        shared.events._eventbridge_client = None
        order_service.models._dynamodb = None
        payment_service.models._dynamodb = None

        yield dynamodb


@pytest.fixture
def sample_order_items():
    """Sample order line items for use in tests."""
    return [
        {"product_id": "prod_001", "quantity": 2, "unit_price": 29.99},
        {"product_id": "prod_002", "quantity": 1, "unit_price": 49.99},
    ]


@pytest.fixture
def lambda_context():
    """Minimal mock of the Lambda context object.

    Lambda handlers receive this as their second argument. We only need
    aws_request_id for correlation ID fallback.
    """

    class MockContext:
        aws_request_id = "test-request-id-12345"
        function_name = "test-function"
        memory_limit_in_mb = 256
        invoked_function_arn = "arn:aws:lambda:us-east-1:123456789:function:test"

    return MockContext()
