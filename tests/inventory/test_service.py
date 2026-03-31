"""Tests for the inventory service."""

import json
import os
import sys
import importlib

import pytest

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_inventory_svc_dir = os.path.join(_project_root, "services", "inventory")


def _reload_inventory_modules():
    """Reload inventory modules so module-level boto3 clients bind to the active moto mock.

    The inventory service uses bare ``from repository import ...`` which requires
    ``services/inventory`` to be first on sys.path while reloading. We temporarily
    prepend it, reload both modules, then remove it to avoid polluting sys.path for
    other service tests that also have a ``repository.py``.
    """
    sys.path.insert(0, _inventory_svc_dir)
    try:
        # Remove any stale bare 'repository' from sys.modules so reload picks up
        # the correct one from the inventory directory.
        sys.modules.pop("repository", None)

        import inventory.repository as repo_mod
        importlib.reload(repo_mod)

        # After reloading inventory.repository, the bare ``repository`` entry in
        # sys.modules now points to inventory's repository. Reload the service
        # which does ``from repository import ...``.
        import inventory.service as svc_mod
        importlib.reload(svc_mod)
    finally:
        try:
            sys.path.remove(_inventory_svc_dir)
        except ValueError:
            pass


class TestCreateProduct:
    def test_initialize_inventory(self, aws_mock, mocker):
        """ProductCreated event should create an inventory record."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import create_product
        from inventory.repository import get_inventory

        create_product("prod_001", 100)

        # Verify inventory was created
        inv = get_inventory("prod_001")
        assert inv is not None
        assert inv["available"] == 100
        assert inv["reserved"] == 0

        # Verify InventoryInitialized event published
        mock_publish.assert_called()
        call_args = mock_publish.call_args
        assert call_args[0][0] == "InventoryInitialized"

    def test_duplicate_product_raises(self, aws_mock, mocker):
        """Creating same product twice should publish InventoryInitializationFailed and raise."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import create_product

        create_product("prod_dup", 50)

        with pytest.raises(ValueError, match="already exists"):
            create_product("prod_dup", 50)

        failed_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "InventoryInitializationFailed"
        ]
        assert len(failed_calls) == 1


class TestReserveInventory:
    def test_reserve_success(self, aws_mock, mocker):
        """OrderCreated should reserve stock and publish InventoryReserved."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import create_product, reserve_inventory
        from inventory.repository import get_inventory

        create_product("prod_r1", 50)

        reserve_inventory("ord_001", [{"productId": "prod_r1", "quantity": 5}])

        # Verify stock was decremented
        inv = get_inventory("prod_r1")
        assert inv["available"] == 45
        assert inv["reserved"] == 5

        # Verify InventoryReserved event
        reserved_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "InventoryReserved"
        ]
        assert len(reserved_calls) == 1

    def test_reserve_insufficient_stock(self, aws_mock, mocker):
        """Reserving more than available should publish InventoryReservationFailed and raise."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import create_product, reserve_inventory

        create_product("prod_r2", 3)

        with pytest.raises(ValueError, match="Insufficient stock"):
            reserve_inventory("ord_002", [{"productId": "prod_r2", "quantity": 10}])

        failed_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "InventoryReservationFailed"
        ]
        assert len(failed_calls) == 1

    def test_reserve_product_not_found(self, aws_mock, mocker):
        """Reserving a nonexistent product should publish InventoryReservationFailed and raise."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import reserve_inventory

        with pytest.raises(ValueError, match="not found"):
            reserve_inventory("ord_003", [{"productId": "prod_missing", "quantity": 1}])

        failed_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "InventoryReservationFailed"
        ]
        assert len(failed_calls) == 1

    def test_reserve_multiple_items(self, aws_mock, mocker):
        """Reserving multiple products in one order should decrement all."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import create_product, reserve_inventory
        from inventory.repository import get_inventory

        create_product("prod_m1", 30)
        create_product("prod_m2", 20)

        reserve_inventory("ord_multi", [
            {"productId": "prod_m1", "quantity": 5},
            {"productId": "prod_m2", "quantity": 3},
        ])

        inv1 = get_inventory("prod_m1")
        assert inv1["available"] == 25
        assert inv1["reserved"] == 5

        inv2 = get_inventory("prod_m2")
        assert inv2["available"] == 17
        assert inv2["reserved"] == 3


class TestLowStockAlert:
    def test_low_stock_event_published(self, aws_mock, mocker):
        """Reserving stock that drops below threshold should publish LowStock."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import create_product, reserve_inventory

        # LOW_STOCK_THRESHOLD is 10 (set in conftest)
        create_product("prod_low", 15)
        reserve_inventory("ord_low", [{"productId": "prod_low", "quantity": 8}])

        # available is now 7, which is below threshold of 10
        low_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "LowStock"
        ]
        assert len(low_calls) == 1

    def test_out_of_stock_event_published(self, aws_mock, mocker):
        """Reserving all remaining stock should publish OutOfStock."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import create_product, reserve_inventory

        create_product("prod_oos", 5)
        reserve_inventory("ord_oos", [{"productId": "prod_oos", "quantity": 5}])

        # available is now 0
        oos_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "OutOfStock"
        ]
        assert len(oos_calls) == 1


class TestReleaseInventory:
    def test_release_reserved_items(self, aws_mock, mocker):
        """OrderCanceled should release reserved stock back to available."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import create_product, reserve_inventory, release_inventory
        from inventory.repository import get_inventory

        create_product("prod_rel", 20)
        reserve_inventory("ord_rel", [{"productId": "prod_rel", "quantity": 5}])

        # Verify stock was decremented
        inv = get_inventory("prod_rel")
        assert inv["available"] == 15
        assert inv["reserved"] == 5

        release_inventory("ord_rel")

        inv = get_inventory("prod_rel")
        assert inv["available"] == 20  # restored
        assert inv["reserved"] == 0

        released_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "InventoryReleased"
        ]
        assert len(released_calls) == 1

    def test_release_no_reservations(self, aws_mock, mocker):
        """Releasing an order with no reservations should still publish InventoryReleased."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import release_inventory

        release_inventory("ord_nonexistent")

        released_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "InventoryReleased"
        ]
        assert len(released_calls) == 1

    def test_compensate_inventory_is_same_as_release(self, aws_mock, mocker):
        """CompensateInventory events use the same release_inventory path."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import create_product, reserve_inventory, release_inventory
        from inventory.repository import get_inventory

        create_product("prod_comp", 40)
        reserve_inventory("ord_comp", [{"productId": "prod_comp", "quantity": 10}])

        # CompensateInventory is handled by calling release_inventory(order_id)
        release_inventory("ord_comp")

        inv = get_inventory("prod_comp")
        assert inv["available"] == 40
        assert inv["reserved"] == 0


class TestFulfillInventory:
    def test_fulfill_marks_shipped(self, aws_mock, mocker):
        """ShipmentCreated should mark reservations as FULFILLED and clear reserved counter."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import create_product, reserve_inventory, fulfill_inventory
        from inventory.repository import get_inventory, get_reservations_by_order

        create_product("prod_ful", 10)
        reserve_inventory("ord_ful", [{"productId": "prod_ful", "quantity": 3}])
        fulfill_inventory("ord_ful")

        inv = get_inventory("prod_ful")
        assert inv["reserved"] == 0  # cleared after fulfillment
        assert inv["available"] == 7  # not restored -- items shipped

        # Reservation status should be FULFILLED
        reservations = get_reservations_by_order("ord_ful")
        assert len(reservations) == 1
        assert reservations[0]["status"] == "FULFILLED"

        fulfilled_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "InventoryFulfilled"
        ]
        assert len(fulfilled_calls) == 1

    def test_fulfill_no_reservations(self, aws_mock, mocker):
        """Fulfilling an order with no reservations should publish InventoryFulfillmentFailed."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import fulfill_inventory

        fulfill_inventory("ord_none")

        failed_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "InventoryFulfillmentFailed"
        ]
        assert len(failed_calls) == 1

    def test_fulfill_idempotent(self, aws_mock, mocker):
        """Calling fulfill twice should skip already-FULFILLED reservations."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import create_product, reserve_inventory, fulfill_inventory
        from inventory.repository import get_inventory

        create_product("prod_idem", 10)
        reserve_inventory("ord_idem", [{"productId": "prod_idem", "quantity": 2}])
        fulfill_inventory("ord_idem")
        fulfill_inventory("ord_idem")  # second call should be a no-op

        inv = get_inventory("prod_idem")
        assert inv["reserved"] == 0
        assert inv["available"] == 8


class TestRestockInventory:
    def test_restock_after_return(self, aws_mock, mocker):
        """OrderReturned should restock fulfilled items back to available."""
        _reload_inventory_modules()
        mock_publish = mocker.patch("inventory.service._publish_event", return_value=None)
        from inventory.service import (
            create_product, reserve_inventory, fulfill_inventory, restock_inventory,
        )
        from inventory.repository import get_inventory

        create_product("prod_ret", 10)
        reserve_inventory("ord_ret", [{"productId": "prod_ret", "quantity": 3}])
        fulfill_inventory("ord_ret")

        inv = get_inventory("prod_ret")
        assert inv["available"] == 7

        restock_inventory("ord_ret", "ret_001", [{"productId": "prod_ret", "quantity": 3}])

        inv = get_inventory("prod_ret")
        assert inv["available"] == 10  # restored after return

        restocked_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "InventoryRestocked"
        ]
        assert len(restocked_calls) == 1
