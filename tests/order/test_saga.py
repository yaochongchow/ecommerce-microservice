"""Tests for the saga state machine."""

import pytest


class TestSagaHappyPath:
    def test_start_saga(self, aws_mock, sample_order_items, mocker):
        mock_publish = mocker.patch("order.saga.publish_event", return_value={"FailedEntryCount": 0})
        from order.models import create_order, create_saga_state
        from order.saga import start_saga

        order = create_order(user_id="usr_saga001", items=sample_order_items)
        create_saga_state(order["order_id"])
        result = start_saga(order, correlation_id="corr-saga-001")

        assert result["current_state"] == "INVENTORY_RESERVING"
        mock_publish.assert_called_once()
        assert mock_publish.call_args[0][0] == "OrderCreated"

    def test_inventory_reserved_advances_to_payment(self, aws_mock, sample_order_items, mocker):
        mocker.patch("shared.events.publish_event", return_value={"FailedEntryCount": 0})
        from order.models import create_order, create_saga_state
        from order.saga import handle_inventory_reserved, start_saga

        order = create_order(user_id="usr_saga002", items=sample_order_items)
        create_saga_state(order["order_id"])
        start_saga(order)

        result = handle_inventory_reserved(order_id=order["order_id"], reservation_id="res_test123")
        assert result["current_state"] == "PAYMENT_PROCESSING"

    def test_payment_completed_confirms_order(self, aws_mock, sample_order_items, mocker):
        mocker.patch("shared.events.publish_event", return_value={"FailedEntryCount": 0})
        from order.models import create_order, create_saga_state, get_order
        from order.saga import handle_inventory_reserved, handle_payment_completed, start_saga

        order = create_order(user_id="usr_saga003", items=sample_order_items)
        create_saga_state(order["order_id"])
        start_saga(order)
        handle_inventory_reserved(order_id=order["order_id"], reservation_id="res_test456")

        result = handle_payment_completed(
            order_id=order["order_id"], payment_id="pay_test789",
            charge_id="ch_test789", amount=109.97,
        )
        assert result["current_state"] == "CONFIRMED"
        assert get_order(order["order_id"])["status"] == "CONFIRMED"


class TestSagaInventoryFailure:
    def test_inventory_failed_cancels_order(self, aws_mock, sample_order_items, mocker):
        mocker.patch("shared.events.publish_event", return_value={"FailedEntryCount": 0})
        from order.models import create_order, create_saga_state, get_order
        from order.saga import handle_inventory_failed, start_saga

        order = create_order(user_id="usr_saga004", items=sample_order_items)
        create_saga_state(order["order_id"])
        start_saga(order)

        result = handle_inventory_failed(order_id=order["order_id"], reason="Insufficient stock")
        assert result["current_state"] == "CANCELLED"
        assert get_order(order["order_id"])["status"] == "CANCELLED"


class TestSagaCompensation:
    def test_payment_failed_triggers_compensation(self, aws_mock, sample_order_items, mocker):
        mocker.patch("order.saga.publish_event", return_value={"FailedEntryCount": 0})
        mock_publish = mocker.patch("order.compensation.publish_event", return_value={"FailedEntryCount": 0})
        from order.models import create_order, create_saga_state, get_saga_state
        from order.saga import handle_inventory_reserved, handle_payment_failed, start_saga

        order = create_order(user_id="usr_saga005", items=sample_order_items)
        create_saga_state(order["order_id"])
        start_saga(order)
        handle_inventory_reserved(order_id=order["order_id"], reservation_id="res_comp001")

        handle_payment_failed(order_id=order["order_id"], reason="Card declined")

        saga_state = get_saga_state(order["order_id"])
        assert saga_state["current_state"] == "COMPENSATING"

        compensate_calls = [c for c in mock_publish.call_args_list if c[0][0] == "CompensateInventory"]
        assert len(compensate_calls) == 1


class TestSagaStateHistory:
    def test_saga_records_transition_history(self, aws_mock, sample_order_items, mocker):
        mocker.patch("shared.events.publish_event", return_value={"FailedEntryCount": 0})
        from order.models import create_order, create_saga_state, get_saga_state
        from order.saga import start_saga

        order = create_order(user_id="usr_saga006", items=sample_order_items)
        create_saga_state(order["order_id"])
        start_saga(order)

        saga_state = get_saga_state(order["order_id"])
        history = saga_state["history"]
        assert len(history) == 2
        assert history[0]["to_state"] == "PENDING"
        assert history[1]["to_state"] == "INVENTORY_RESERVING"
