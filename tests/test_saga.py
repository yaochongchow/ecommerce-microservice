"""
Tests for the saga state machine.

These tests verify the saga transitions through the correct states
(inventory-first flow):
  - Happy path: PENDING -> INVENTORY_RESERVING -> INVENTORY_RESERVED
                -> PAYMENT_PROCESSING -> CONFIRMED
  - Inventory failure: PENDING -> INVENTORY_RESERVING -> INVENTORY_FAILED -> CANCELLED
  - Payment failure with compensation: PENDING -> ... -> PAYMENT_FAILED
                -> COMPENSATING -> CANCELLED
"""

import pytest


class TestSagaHappyPath:
    """Tests for the successful order flow."""

    def test_start_saga(self, aws_mock, sample_order_items, mocker):
        """Starting the saga should transition to INVENTORY_RESERVING and publish order.created."""
        mock_publish = mocker.patch(
            "order_service.saga.publish_event",
            return_value={"FailedEntryCount": 0},
        )

        from order_service.models import create_order, create_saga_state, get_saga_state
        from order_service.saga import start_saga

        order = create_order(user_id="usr_saga001", items=sample_order_items)
        create_saga_state(order["order_id"])

        result = start_saga(order, correlation_id="corr-saga-001")

        assert result["current_state"] == "INVENTORY_RESERVING"

        # Verify order.created event was published (for M4 inventory)
        mock_publish.assert_called_once()
        assert mock_publish.call_args[0][0] == "order.created"

    def test_inventory_reserved_advances_to_payment(self, aws_mock, sample_order_items, mocker):
        """After inventory is reserved, saga should advance to PAYMENT_PROCESSING."""
        mocker.patch(
            "shared.events.publish_event",
            return_value={"FailedEntryCount": 0},
        )

        from order_service.models import create_order, create_saga_state
        from order_service.saga import handle_inventory_reserved, start_saga

        order = create_order(user_id="usr_saga002", items=sample_order_items)
        create_saga_state(order["order_id"])
        start_saga(order)

        result = handle_inventory_reserved(
            order_id=order["order_id"],
            reservation_id="res_test123",
        )

        assert result["current_state"] == "PAYMENT_PROCESSING"

    def test_payment_completed_confirms_order(self, aws_mock, sample_order_items, mocker):
        """After payment completes, saga should reach CONFIRMED."""
        mocker.patch(
            "shared.events.publish_event",
            return_value={"FailedEntryCount": 0},
        )

        from order_service.models import create_order, create_saga_state, get_order
        from order_service.saga import (
            handle_inventory_reserved,
            handle_payment_completed,
            start_saga,
        )

        order = create_order(user_id="usr_saga003", items=sample_order_items)
        create_saga_state(order["order_id"])
        start_saga(order)
        handle_inventory_reserved(
            order_id=order["order_id"],
            reservation_id="res_test456",
        )

        result = handle_payment_completed(
            order_id=order["order_id"],
            payment_id="pay_test789",
            charge_id="ch_test789",
            amount=109.97,
        )

        assert result["current_state"] == "CONFIRMED"

        # Verify the order itself is also CONFIRMED
        updated_order = get_order(order["order_id"])
        assert updated_order["status"] == "CONFIRMED"


class TestSagaInventoryFailure:
    """Tests for the inventory failure flow."""

    def test_inventory_failed_cancels_order(self, aws_mock, sample_order_items, mocker):
        """When inventory fails, the saga should cancel the order (no compensation)."""
        mocker.patch(
            "shared.events.publish_event",
            return_value={"FailedEntryCount": 0},
        )

        from order_service.models import create_order, create_saga_state, get_order
        from order_service.saga import handle_inventory_failed, start_saga

        order = create_order(user_id="usr_saga004", items=sample_order_items)
        create_saga_state(order["order_id"])
        start_saga(order)

        result = handle_inventory_failed(
            order_id=order["order_id"],
            reason="Insufficient stock",
        )

        assert result["current_state"] == "CANCELLED"

        # Verify the order is cancelled
        updated_order = get_order(order["order_id"])
        assert updated_order["status"] == "CANCELLED"


class TestSagaCompensation:
    """Tests for the payment failure -> compensation flow."""

    def test_payment_failed_triggers_compensation(self, aws_mock, sample_order_items, mocker):
        """When payment fails after inventory reserved, compensation should release inventory."""
        mocker.patch(
            "order_service.saga.publish_event",
            return_value={"FailedEntryCount": 0},
        )
        mock_publish = mocker.patch(
            "order_service.compensation.publish_event",
            return_value={"FailedEntryCount": 0},
        )

        from order_service.models import create_order, create_saga_state, get_saga_state
        from order_service.saga import (
            handle_inventory_reserved,
            handle_payment_failed,
            start_saga,
        )

        order = create_order(user_id="usr_saga005", items=sample_order_items)
        create_saga_state(order["order_id"])
        start_saga(order)
        handle_inventory_reserved(
            order_id=order["order_id"],
            reservation_id="res_comp001",
        )

        result = handle_payment_failed(
            order_id=order["order_id"],
            reason="Card declined",
        )

        # Saga should be in COMPENSATING state
        saga_state = get_saga_state(order["order_id"])
        assert saga_state["current_state"] == "COMPENSATING"

        # Verify saga.compensate_inventory event was published
        publish_calls = mock_publish.call_args_list
        compensate_calls = [c for c in publish_calls if c[0][0] == "saga.compensate_inventory"]
        assert len(compensate_calls) == 1


class TestSagaStateHistory:
    """Tests for saga state history tracking."""

    def test_saga_records_transition_history(self, aws_mock, sample_order_items, mocker):
        """Every state transition should be recorded in the saga history."""
        mocker.patch(
            "shared.events.publish_event",
            return_value={"FailedEntryCount": 0},
        )

        from order_service.models import create_order, create_saga_state, get_saga_state
        from order_service.saga import start_saga

        order = create_order(user_id="usr_saga006", items=sample_order_items)
        create_saga_state(order["order_id"])
        start_saga(order)

        saga_state = get_saga_state(order["order_id"])
        history = saga_state["history"]

        # Should have 2 entries: initial PENDING + transition to INVENTORY_RESERVING
        assert len(history) == 2
        assert history[0]["to_state"] == "PENDING"
        assert history[1]["from_state"] == "PENDING"
        assert history[1]["to_state"] == "INVENTORY_RESERVING"
