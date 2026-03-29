"""
Local demo script -- runs the saga flow with mocked AWS and prints
DynamoDB table contents at each step in a readable format.

Saga flow (inventory-first):
    Happy path:    PENDING -> INVENTORY_RESERVING -> INVENTORY_RESERVED -> PAYMENT_PROCESSING -> CONFIRMED
    Inventory fail: PENDING -> INVENTORY_RESERVING -> INVENTORY_FAILED -> CANCELLED
    Payment fail:  PENDING -> INVENTORY_RESERVING -> INVENTORY_RESERVED -> PAYMENT_PROCESSING
                       -> PAYMENT_FAILED -> COMPENSATING -> CANCELLED

Usage:
    python3 demo_local.py
"""

import json
import os
import sys
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Set environment variables before importing application code
# ---------------------------------------------------------------------------
os.environ["ORDERS_TABLE"] = "demo-orders"
os.environ["SAGA_STATE_TABLE"] = "demo-saga-state"
os.environ["PAYMENTS_TABLE"] = "demo-payments"
os.environ["IDEMPOTENCY_TABLE"] = "demo-idempotency-keys"
os.environ["EVENT_BUS_NAME"] = "demo-event-bus"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"

import boto3
from moto import mock_aws


# ---------------------------------------------------------------------------
# Table formatting helpers
# ---------------------------------------------------------------------------

def print_header(title):
    """Print a section header."""
    width = 80
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def print_step(step_num, description):
    """Print a step header."""
    print()
    print(f"\n{'─' * 80}")
    print(f"  STEP {step_num}: {description}")
    print(f"{'─' * 80}")


def print_table(table_name, items, highlight_fields=None):
    """Print a DynamoDB table's contents in a formatted table.

    Args:
        table_name: Display name for the table.
        items: List of DynamoDB item dicts.
        highlight_fields: Fields to show first (in order). Remaining fields follow.
    """
    if not items:
        print(f"\n  📋 {table_name}: (empty)")
        return

    print(f"\n  📋 {table_name} ({len(items)} record{'s' if len(items) != 1 else ''}):")
    print(f"  {'─' * 74}")

    for i, item in enumerate(items):
        if i > 0:
            print(f"  {'· ' * 37}")

        # Determine field order: highlighted fields first, then the rest
        if highlight_fields:
            ordered_keys = [k for k in highlight_fields if k in item]
            ordered_keys += [k for k in item if k not in ordered_keys]
        else:
            ordered_keys = list(item.keys())

        for key in ordered_keys:
            value = item[key]
            # Format the value for display
            formatted = _format_value(key, value)
            # Color-code status fields
            display_val = _colorize(key, formatted)
            print(f"  │ {key:<22} │ {display_val}")

    print(f"  {'─' * 74}")


def _format_value(key, value):
    """Format a DynamoDB value for display."""
    if value is None:
        return "—"
    if isinstance(value, list):
        if key == "history":
            # Format saga history as a compact timeline
            lines = []
            for entry in value:
                from_s = entry.get("from_state") or "∅"
                to_s = entry.get("to_state", "?")
                reason = entry.get("reason", "")
                ts = entry.get("timestamp", "")[:19]
                lines.append(f"{from_s} → {to_s}  ({reason})")
            return "\n" + "\n".join(f"  │ {'':22} │   {l}" for l in lines)
        if key == "items":
            # Format order items
            lines = []
            for item in value:
                pid = item.get("product_id", "?")
                qty = item.get("quantity", 0)
                price = item.get("unit_price", 0)
                lines.append(f"{pid} × {qty} @ ${float(price):.2f}")
            return ", ".join(lines)
        return json.dumps(value, default=str)
    if isinstance(value, dict):
        if not value:
            return "—"
        parts = [f"{k}: {v}" for k, v in value.items()]
        return ", ".join(parts)
    return str(value)


def _colorize(key, value):
    """Add ANSI color codes to status values."""
    if key == "status" or key == "current_state":
        status = str(value)
        colors = {
            "PENDING": "\033[33m",            # Yellow
            "INVENTORY_RESERVING": "\033[36m",  # Cyan
            "INVENTORY_RESERVED": "\033[36m",   # Cyan
            "PAYMENT_PROCESSING": "\033[36m",  # Cyan
            "CONFIRMED": "\033[32m",           # Green
            "COMPLETED": "\033[32m",           # Green
            "CANCELLED": "\033[31m",           # Red
            "INVENTORY_FAILED": "\033[31m",    # Red
            "PAYMENT_FAILED": "\033[31m",      # Red
            "COMPENSATING": "\033[35m",        # Magenta
            "REFUNDED": "\033[35m",            # Magenta
        }
        color = colors.get(status, "")
        reset = "\033[0m" if color else ""
        return f"{color}{status}{reset}"
    return value


def scan_table(dynamodb, table_name):
    """Scan all items from a DynamoDB table."""
    table = dynamodb.Table(table_name)
    return table.scan().get("Items", [])


def print_all_tables(dynamodb):
    """Print all 4 DynamoDB tables."""
    orders = scan_table(dynamodb, "demo-orders")
    print_table(
        "Orders Table",
        orders,
        highlight_fields=["order_id", "user_id", "status", "total_amount", "items"],
    )

    saga = scan_table(dynamodb, "demo-saga-state")
    print_table(
        "Saga State Table",
        saga,
        highlight_fields=["order_id", "current_state", "reservation_id", "payment_id", "charge_id", "history"],
    )

    payments = scan_table(dynamodb, "demo-payments")
    print_table(
        "Payments Table",
        payments,
        highlight_fields=["payment_id", "order_id", "status", "amount", "charge_id", "refund_id"],
    )

    idempotency = scan_table(dynamodb, "demo-idempotency-keys")
    print_table(
        "Idempotency Keys Table",
        idempotency,
        highlight_fields=["idempotency_key", "created_at"],
    )


# ---------------------------------------------------------------------------
# Setup: Create mocked AWS resources
# ---------------------------------------------------------------------------

def create_tables(dynamodb):
    """Create all DynamoDB tables (same schema as template.yaml)."""
    dynamodb.create_table(
        TableName="demo-orders",
        KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName="demo-saga-state",
        KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName="demo-payments",
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
        TableName="demo-idempotency-keys",
        KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    # Create EventBridge bus
    events_client = boto3.client("events", region_name="us-east-1")
    events_client.create_event_bus(Name="demo-event-bus")


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------

@mock_aws
def run_demo():
    """Run the full saga demo with formatted table output."""

    # Setup
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    create_tables(dynamodb)

    # Reset lazy-initialized clients
    import shared.events
    import order_service.models
    import payment_service.models
    shared.events._eventbridge_client = None
    order_service.models._dynamodb = None
    payment_service.models._dynamodb = None

    # Import application code
    from order_service.models import create_order, create_saga_state, get_order
    from order_service.saga import (
        start_saga,
        handle_inventory_reserved,
        handle_inventory_failed,
        handle_payment_completed,
        handle_payment_failed,
    )
    from order_service.compensation import handle_inventory_released
    from payment_service.models import create_payment

    # -----------------------------------------------------------------------
    # Helper: print all tables filtered to a single order
    # -----------------------------------------------------------------------
    def print_order_tables(dynamodb, order_id, label=""):
        """Print all 4 tables filtered to a single order's data."""
        suffix = f" ({label})" if label else ""

        orders = scan_table(dynamodb, "demo-orders")
        order_items = [o for o in orders if o["order_id"] == order_id]
        print_table(
            f"Orders Table{suffix}", order_items,
            highlight_fields=["order_id", "user_id", "status", "total_amount", "items",
                              "reservation_id", "cancellation_reason"],
        )

        saga = scan_table(dynamodb, "demo-saga-state")
        saga_items = [s for s in saga if s["order_id"] == order_id]
        print_table(
            f"Saga State Table{suffix}", saga_items,
            highlight_fields=["order_id", "current_state", "reservation_id", "payment_id", "charge_id", "history"],
        )

        payments = scan_table(dynamodb, "demo-payments")
        pay_items = [p for p in payments if p["order_id"] == order_id]
        print_table(
            f"Payments Table{suffix}", pay_items,
            highlight_fields=["payment_id", "order_id", "status", "amount", "charge_id",
                              "refund_id", "refund_amount"],
        )

        idempotency = scan_table(dynamodb, "demo-idempotency-keys")
        print_table(f"Idempotency Keys Table{suffix}", idempotency,
                    highlight_fields=["idempotency_key", "created_at"])

    # -----------------------------------------------------------------------
    print_header("DEMO: Order & Payment Saga — Happy Path (inventory-first)")
    # -----------------------------------------------------------------------

    # Step 1: Create Order
    print_step(1, "Create Order")
    order = create_order(
        user_id="usr_demo001",
        items=[
            {"product_id": "prod_001", "quantity": 2, "unit_price": 29.99},
            {"product_id": "prod_002", "quantity": 1, "unit_price": 49.99},
        ],
        shipping_address={"street": "123 Huntington Ave", "city": "Boston", "state": "MA", "zip": "02115"},
    )
    create_saga_state(order["order_id"])
    print(f"\n  ✅ Order created: {order['order_id']}")
    print_all_tables(dynamodb)

    # Step 2: Start Saga (publishes order.created → M4 inventory)
    print_step(2, "Start Saga → INVENTORY_RESERVING")
    start_saga(order, correlation_id="demo-corr-001")
    print_all_tables(dynamodb)

    # Step 3: Inventory Reserved → advance to payment
    print_step(3, "Inventory Reserved → PAYMENT_PROCESSING")
    handle_inventory_reserved(
        order_id=order["order_id"],
        reservation_id="res_inv_demo_001",
        correlation_id="demo-corr-001",
    )
    print_all_tables(dynamodb)

    # Step 4: Payment Completed → Order Confirmed
    print_step(4, "Payment Completed → CONFIRMED")
    # Simulate the payment service creating a payment record
    payment = create_payment(
        order_id=order["order_id"],
        amount=float(order["total_amount"]),
        currency="USD",
        charge_id="ch_stripe_demo_001",
        idempotency_key=order["idempotency_key"],
    )
    handle_payment_completed(
        order_id=order["order_id"],
        payment_id=payment["payment_id"],
        charge_id="ch_stripe_demo_001",
        amount=float(order["total_amount"]),
        correlation_id="demo-corr-001",
    )
    print_all_tables(dynamodb)

    # -----------------------------------------------------------------------
    print_header("DEMO: Inventory Failure — Out of Stock (no compensation)")
    # -----------------------------------------------------------------------

    # Reset clients for fresh tables
    order_service.models._dynamodb = None

    print_step(1, "Create Order")
    order2 = create_order(
        user_id="usr_demo002",
        items=[{"product_id": "prod_out_of_stock", "quantity": 100, "unit_price": 9.99}],
    )
    create_saga_state(order2["order_id"])
    print(f"\n  ✅ Order created: {order2['order_id']}")
    print_order_tables(dynamodb, order2["order_id"], "order 2")

    print_step(2, "Start Saga → INVENTORY_RESERVING")
    start_saga(order2, correlation_id="demo-corr-002")
    print_order_tables(dynamodb, order2["order_id"], "order 2")

    print_step(3, "Inventory Failed → CANCELLED (no compensation needed)")
    handle_inventory_failed(
        order_id=order2["order_id"],
        reason="Insufficient stock for prod_out_of_stock",
        correlation_id="demo-corr-002",
    )
    print_order_tables(dynamodb, order2["order_id"], "order 2")

    # -----------------------------------------------------------------------
    print_header("DEMO: Payment Failure — Compensation (release inventory)")
    # -----------------------------------------------------------------------

    print_step(1, "Create Order")
    order3 = create_order(
        user_id="usr_demo003",
        items=[{"product_id": "prod_003", "quantity": 1, "unit_price": 99.99}],
    )
    create_saga_state(order3["order_id"])
    print(f"\n  ✅ Order created: {order3['order_id']}")
    print_order_tables(dynamodb, order3["order_id"], "order 3")

    print_step(2, "Start Saga → INVENTORY_RESERVING")
    start_saga(order3, correlation_id="demo-corr-003")
    print_order_tables(dynamodb, order3["order_id"], "order 3")

    print_step(3, "Inventory Reserved → PAYMENT_PROCESSING")
    handle_inventory_reserved(
        order_id=order3["order_id"],
        reservation_id="res_inv_demo_003",
        correlation_id="demo-corr-003",
    )
    print_order_tables(dynamodb, order3["order_id"], "order 3")

    print_step(4, "Payment Failed → COMPENSATING (inventory release triggered)")
    handle_payment_failed(
        order_id=order3["order_id"],
        reason="Card declined",
        correlation_id="demo-corr-003",
    )
    print_order_tables(dynamodb, order3["order_id"], "order 3")

    print_step(5, "Inventory Released → CANCELLED")
    handle_inventory_released(
        order_id=order3["order_id"],
        correlation_id="demo-corr-003",
    )
    print_order_tables(dynamodb, order3["order_id"], "order 3 — final")

    # -----------------------------------------------------------------------
    print_header("DEMO COMPLETE")
    # -----------------------------------------------------------------------
    print("\n  All 3 scenarios ran successfully:")
    print("    1. Happy path      → PENDING → INVENTORY_RESERVING → ... → CONFIRMED")
    print("    2. Inventory fail  → PENDING → INVENTORY_RESERVING → CANCELLED (no compensation)")
    print("    3. Payment fail    → PENDING → ... → PAYMENT_FAILED → COMPENSATING → CANCELLED")
    print()


if __name__ == "__main__":
    run_demo()
