"""
Tests that verify data in mock DynamoDB tables.

These tests create orders and payments, then query the tables directly
to inspect what got stored. Useful for understanding the data model
and verifying table contents without deploying to AWS.
"""

import json
from decimal import Decimal

import boto3
import pytest


class TestOrdersTable:
    """Inspect the Orders table after creating orders."""

    def test_order_stored_correctly(self, aws_mock, sample_order_items, mocker):
        """Verify the full order record structure in DynamoDB."""
        mocker.patch("order_service.saga.publish_event", return_value={"FailedEntryCount": 0})

        from order_service.models import create_order, create_saga_state
        from order_service.saga import start_saga

        # Create an order and start the saga
        order = create_order(
            user_id="usr_table_test",
            items=sample_order_items,
            shipping_address={"street": "123 Main St", "city": "Boston", "state": "MA", "zip": "02115"},
        )
        create_saga_state(order["order_id"])
        start_saga(order)

        # Query the Orders table directly
        table = aws_mock.Table("test-orders")
        result = table.get_item(Key={"order_id": order["order_id"]})
        stored_order = result["Item"]

        print("\n========== ORDERS TABLE ==========")
        print(json.dumps(stored_order, indent=2, default=str))
        print("==================================\n")

        # Verify structure
        assert stored_order["order_id"] == order["order_id"]
        assert stored_order["user_id"] == "usr_table_test"
        assert stored_order["status"] == "PENDING"
        assert stored_order["currency"] == "USD"
        assert len(stored_order["items"]) == 2
        assert stored_order["shipping_address"]["city"] == "Boston"
        assert "created_at" in stored_order
        assert "idempotency_key" in stored_order

        # Verify total is calculated correctly: (2 * 29.99) + (1 * 49.99) = 109.97
        assert stored_order["total_amount"] == Decimal("109.97")

    def test_scan_all_orders(self, aws_mock, sample_order_items, mocker):
        """Create multiple orders and scan the entire table."""
        mocker.patch("order_service.saga.publish_event", return_value={"FailedEntryCount": 0})

        from order_service.models import create_order

        # Create 3 orders
        orders = []
        for i in range(3):
            order = create_order(user_id=f"usr_{i}", items=sample_order_items)
            orders.append(order)

        # Scan the entire Orders table
        table = aws_mock.Table("test-orders")
        scan_result = table.scan()

        print("\n========== ALL ORDERS (scan) ==========")
        for item in scan_result["Items"]:
            print(f"  {item['order_id']}  |  user: {item['user_id']}  |  status: {item['status']}  |  total: ${item['total_amount']}")
        print(f"\n  Total records: {scan_result['Count']}")
        print("========================================\n")

        assert scan_result["Count"] == 3


class TestSagaStateTable:
    """Inspect the Saga State table to see saga progression."""

    def test_saga_state_after_start(self, aws_mock, sample_order_items, mocker):
        """Verify saga state and history after starting the saga."""
        mocker.patch("order_service.saga.publish_event", return_value={"FailedEntryCount": 0})

        from order_service.models import create_order, create_saga_state
        from order_service.saga import start_saga

        order = create_order(user_id="usr_saga_table", items=sample_order_items)
        create_saga_state(order["order_id"])
        start_saga(order)

        # Query the Saga State table
        table = aws_mock.Table("test-saga-state")
        result = table.get_item(Key={"order_id": order["order_id"]})
        saga = result["Item"]

        print("\n========== SAGA STATE TABLE ==========")
        print(f"  Order ID:      {saga['order_id']}")
        print(f"  Current State: {saga['current_state']}")
        print(f"  Reservation ID:{saga.get('reservation_id', 'N/A')}")
        print(f"  Payment ID:    {saga.get('payment_id', 'N/A')}")
        print(f"  Charge ID:     {saga.get('charge_id', 'N/A')}")
        print(f"\n  History ({len(saga['history'])} transitions):")
        for entry in saga["history"]:
            from_s = str(entry.get('from_state') or 'None')
            to_s = str(entry['to_state'])
            print(f"    {from_s:25s} -> {to_s:25s}  |  {entry['reason']}")
        print("======================================\n")

        assert saga["current_state"] == "INVENTORY_RESERVING"
        assert len(saga["history"]) == 2

    def test_saga_full_happy_path(self, aws_mock, sample_order_items, mocker):
        """Walk through the entire happy path and inspect final saga state."""
        mocker.patch("order_service.saga.publish_event", return_value={"FailedEntryCount": 0})

        from order_service.models import create_order, create_saga_state
        from order_service.saga import (
            handle_inventory_reserved,
            handle_payment_completed,
            start_saga,
        )

        order = create_order(user_id="usr_happy", items=sample_order_items)
        create_saga_state(order["order_id"])
        start_saga(order)
        handle_inventory_reserved(
            order_id=order["order_id"],
            reservation_id="res_mock001",
        )
        handle_payment_completed(
            order_id=order["order_id"],
            payment_id="pay_mock001",
            charge_id="ch_mock001",
            amount=109.97,
        )

        # Query final saga state
        table = aws_mock.Table("test-saga-state")
        result = table.get_item(Key={"order_id": order["order_id"]})
        saga = result["Item"]

        print("\n========== SAGA -- FULL HAPPY PATH ==========")
        print(f"  Order ID:       {saga['order_id']}")
        print(f"  Current State:  {saga['current_state']}")
        print(f"  Reservation ID: {saga.get('reservation_id', 'N/A')}")
        print(f"  Payment ID:     {saga.get('payment_id', 'N/A')}")
        print(f"  Charge ID:      {saga.get('charge_id', 'N/A')}")
        print(f"\n  History ({len(saga['history'])} transitions):")
        for entry in saga["history"]:
            print(f"    {str(entry.get('from_state', 'None')):25s} -> {entry['to_state']:25s}  |  {entry['reason']}")
        print("=============================================\n")

        # Also check the order status
        orders_table = aws_mock.Table("test-orders")
        order_result = orders_table.get_item(Key={"order_id": order["order_id"]})
        final_order = order_result["Item"]

        print("========== ORDER -- FINAL STATE ==========")
        print(f"  Order ID:       {final_order['order_id']}")
        print(f"  Status:         {final_order['status']}")
        print(f"  Reservation ID: {final_order.get('reservation_id', 'N/A')}")
        print(f"  Payment ID:     {final_order.get('payment_id', 'N/A')}")
        print(f"  Charge ID:      {final_order.get('charge_id', 'N/A')}")
        print(f"  Total:          ${final_order['total_amount']}")
        print("==========================================\n")

        assert saga["current_state"] == "CONFIRMED"
        assert final_order["status"] == "CONFIRMED"

    def test_saga_compensation_path(self, aws_mock, sample_order_items, mocker):
        """Walk through the compensation path and inspect saga state."""
        mocker.patch("order_service.saga.publish_event", return_value={"FailedEntryCount": 0})
        mocker.patch("order_service.compensation.publish_event", return_value={"FailedEntryCount": 0})

        from order_service.compensation import handle_inventory_released
        from order_service.models import create_order, create_saga_state
        from order_service.saga import (
            handle_inventory_reserved,
            handle_payment_failed,
            start_saga,
        )

        order = create_order(user_id="usr_comp", items=sample_order_items)
        create_saga_state(order["order_id"])
        start_saga(order)
        handle_inventory_reserved(
            order_id=order["order_id"],
            reservation_id="res_comp_mock",
        )

        # Payment fails -> triggers compensation (release inventory)
        handle_payment_failed(
            order_id=order["order_id"],
            reason="Card declined",
        )

        # Simulate inventory released by M4
        handle_inventory_released(
            order_id=order["order_id"],
        )

        # Query final saga state
        table = aws_mock.Table("test-saga-state")
        result = table.get_item(Key={"order_id": order["order_id"]})
        saga = result["Item"]

        print("\n========== SAGA -- COMPENSATION PATH ==========")
        print(f"  Order ID:      {saga['order_id']}")
        print(f"  Current State: {saga['current_state']}")
        print(f"\n  History ({len(saga['history'])} transitions):")
        for entry in saga["history"]:
            print(f"    {str(entry.get('from_state', 'None')):25s} -> {entry['to_state']:25s}  |  {entry['reason']}")
        print("================================================\n")

        # Also check order
        orders_table = aws_mock.Table("test-orders")
        final_order = orders_table.get_item(Key={"order_id": order["order_id"]})["Item"]

        print("========== ORDER -- CANCELLED STATE ==========")
        print(f"  Status:   {final_order['status']}")
        print(f"  Reason:   {final_order.get('cancellation_reason', 'N/A')}")
        print("==============================================\n")

        assert saga["current_state"] == "CANCELLED"
        assert final_order["status"] == "CANCELLED"


class TestPaymentsTable:
    """Inspect the Payments and Idempotency tables."""

    def test_payment_stored_after_charge(self, aws_mock, lambda_context, mocker):
        """Verify the payment record and idempotency key after a successful charge."""
        mocker.patch(
            "payment_service.stripe_client.stripe.Charge.create",
            return_value=mocker.Mock(id="ch_table_test", status="succeeded"),
        )
        mocker.patch("payment_service.handler.publish_event", return_value={"FailedEntryCount": 0})

        from payment_service.handler import event_handler

        event = {
            "detail-type": "order.ready_for_payment",
            "detail": {
                "metadata": {"correlation_id": "corr-table-test"},
                "data": {
                    "order_id": "ord_table_test",
                    "user_id": "usr_table_test",
                    "items": [{"product_id": "p1", "quantity": 1, "unit_price": 49.99}],
                    "total_amount": 49.99,
                    "currency": "USD",
                    "idempotency_key": "idem_table_test",
                },
            },
        }
        event_handler(event, lambda_context)

        # Query the Payments table
        payments_table = aws_mock.Table("test-payments")
        scan = payments_table.scan()

        print("\n========== PAYMENTS TABLE ==========")
        for p in scan["Items"]:
            print(f"  Payment ID:      {p['payment_id']}")
            print(f"  Order ID:        {p['order_id']}")
            print(f"  Charge ID:       {p['charge_id']}")
            print(f"  Amount:          ${p['amount']}")
            print(f"  Currency:        {p['currency']}")
            print(f"  Status:          {p['status']}")
            print(f"  Idempotency Key: {p['idempotency_key']}")
            print(f"  Refund ID:       {p.get('refund_id', 'N/A')}")
            print(f"  Created At:      {p['created_at']}")
        print(f"\n  Total records: {scan['Count']}")
        print("====================================\n")

        # Query the Idempotency Keys table
        idem_table = aws_mock.Table("test-idempotency-keys")
        idem_scan = idem_table.scan()

        print("========== IDEMPOTENCY KEYS TABLE ==========")
        for k in idem_scan["Items"]:
            print(f"  Key:        {k['idempotency_key']}")
            print(f"  Created At: {k['created_at']}")
            print(f"  TTL:        {k['ttl']}")
            cached = k.get("cached_result", {})
            print(f"  Cached Payment ID: {cached.get('payment_id', 'N/A')}")
        print(f"\n  Total records: {idem_scan['Count']}")
        print("=============================================\n")

        assert scan["Count"] == 1
        assert scan["Items"][0]["order_id"] == "ord_table_test"
        assert scan["Items"][0]["charge_id"] == "ch_table_test"
        assert idem_scan["Count"] == 1

    def test_payment_refund_updates_record(self, aws_mock, lambda_context, mocker):
        """Verify the payment record after a refund."""
        mocker.patch(
            "payment_service.stripe_client.stripe.Charge.create",
            return_value=mocker.Mock(id="ch_refund_table", status="succeeded"),
        )
        mocker.patch(
            "payment_service.stripe_client.stripe.Refund.create",
            return_value=mocker.Mock(id="re_table_test", amount=4999, status="succeeded"),
        )
        mocker.patch("payment_service.handler.publish_event", return_value={"FailedEntryCount": 0})

        from payment_service.handler import event_handler

        # Step 1: Create the payment
        event_handler({
            "detail-type": "order.ready_for_payment",
            "detail": {
                "metadata": {"correlation_id": "corr-refund-table"},
                "data": {
                    "order_id": "ord_refund_table",
                    "user_id": "usr_refund_table",
                    "items": [{"product_id": "p1", "quantity": 1, "unit_price": 49.99}],
                    "total_amount": 49.99,
                    "currency": "USD",
                    "idempotency_key": "idem_refund_table",
                },
            },
        }, lambda_context)

        # Step 2: Refund the payment
        event_handler({
            "detail-type": "saga.compensate_payment",
            "detail": {
                "metadata": {"correlation_id": "corr-refund-table"},
                "data": {
                    "order_id": "ord_refund_table",
                    "charge_id": "ch_refund_table",
                    "payment_id": "pay_x",
                    "amount": 49.99,
                    "reason": "Post-confirmation cancellation",
                },
            },
        }, lambda_context)

        # Query the Payments table -- should show REFUNDED status
        payments_table = aws_mock.Table("test-payments")
        scan = payments_table.scan()

        print("\n========== PAYMENT AFTER REFUND ==========")
        for p in scan["Items"]:
            print(f"  Payment ID:    {p['payment_id']}")
            print(f"  Order ID:      {p['order_id']}")
            print(f"  Charge ID:     {p['charge_id']}")
            print(f"  Amount:        ${p['amount']}")
            print(f"  Status:        {p['status']}")
            print(f"  Refund ID:     {p.get('refund_id', 'N/A')}")
            print(f"  Refund Amount: ${p.get('refund_amount', 'N/A')}")
        print("==========================================\n")

        refunded = scan["Items"][0]
        assert refunded["status"] == "REFUNDED"
        assert refunded["refund_id"] == "re_table_test"
