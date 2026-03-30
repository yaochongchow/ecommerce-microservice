"""Tests for the shipping service."""

import os
import sys
import importlib

import pytest

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_shipping_svc_dir = os.path.join(_project_root, "services", "shipping")


def _reload_shipping_modules():
    """Reload shipping modules so module-level boto3 clients bind to the active moto mock.

    The shipping service uses bare ``from repository import ...`` which requires
    ``services/shipping`` to be first on sys.path while reloading. We temporarily
    prepend it, reload both modules, then remove it to avoid polluting sys.path for
    other service tests that also have a ``repository.py``.
    """
    sys.path.insert(0, _shipping_svc_dir)
    try:
        # Remove any stale bare 'repository' from sys.modules so reload picks up
        # the correct one from the shipping directory.
        sys.modules.pop("repository", None)

        import shipping.repository as repo_mod
        importlib.reload(repo_mod)

        import shipping.service as svc_mod
        importlib.reload(svc_mod)
    finally:
        try:
            sys.path.remove(_shipping_svc_dir)
        except ValueError:
            pass


class TestCreateShipment:
    def test_create_shipment_success(self, aws_mock, mocker):
        """OrderConfirmed event should create a shipment with tracking number."""
        _reload_shipping_modules()
        mock_publish = mocker.patch("shipping.service._publish_event", return_value=None)
        from shipping.service import create_shipment

        result = create_shipment(
            order_id="ord_ship001",
            email="alice@test.com",
            shipping_address={"street": "123 Main St", "city": "Boston"},
            items=[{"productId": "p1", "quantity": 2}],
        )

        assert result["orderId"] == "ord_ship001"
        assert result["shipmentId"].startswith("shp_")
        assert result["trackingNumber"].startswith("MOCK-")
        assert result["status"] == "LABEL_CREATED"
        assert result["carrier"] == "UPS_MOCK"
        assert result["email"] == "alice@test.com"

        # Verify ShipmentCreated event published
        published_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "ShipmentCreated"
        ]
        assert len(published_calls) == 1

        # Verify the event detail contains expected fields
        event_detail = published_calls[0][0][1]
        assert event_detail["orderId"] == "ord_ship001"
        assert "trackingNumber" in event_detail
        assert "shipmentId" in event_detail

    def test_idempotent_duplicate_shipment(self, aws_mock, mocker):
        """Duplicate OrderConfirmed should return cached shipment, not create new one."""
        _reload_shipping_modules()
        mock_publish = mocker.patch("shipping.service._publish_event", return_value=None)
        from shipping.service import create_shipment

        result1 = create_shipment(
            order_id="ord_dup_ship",
            email="bob@test.com",
            shipping_address={"street": "456 Elm St"},
            items=[{"productId": "p2", "quantity": 1}],
        )
        result2 = create_shipment(
            order_id="ord_dup_ship",
            email="bob@test.com",
            shipping_address={"street": "456 Elm St"},
            items=[{"productId": "p2", "quantity": 1}],
        )

        # Same shipment ID returned
        assert result1["shipmentId"] == result2["shipmentId"]
        assert result1["trackingNumber"] == result2["trackingNumber"]

        # ShipmentCreated should have been published twice (once fresh, once republished)
        published_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "ShipmentCreated"
        ]
        assert len(published_calls) == 2

    def test_shipment_stored_in_dynamodb(self, aws_mock, mocker):
        """Created shipment should be retrievable from the repository."""
        _reload_shipping_modules()
        mocker.patch("shipping.service._publish_event", return_value=None)
        from shipping.service import create_shipment
        from shipping.repository import get_shipment_by_order

        create_shipment(
            order_id="ord_stored",
            email="charlie@test.com",
            shipping_address={"city": "NYC"},
            items=[],
        )

        stored = get_shipment_by_order("ord_stored")
        assert stored is not None
        assert stored["orderId"] == "ord_stored"
        assert stored["email"] == "charlie@test.com"

    def test_shipment_contains_items(self, aws_mock, mocker):
        """Shipment record should contain the order items."""
        _reload_shipping_modules()
        mocker.patch("shipping.service._publish_event", return_value=None)
        from shipping.service import create_shipment

        items = [
            {"productId": "p1", "quantity": 2},
            {"productId": "p2", "quantity": 1},
        ]
        result = create_shipment(
            order_id="ord_items",
            email="dave@test.com",
            shipping_address={},
            items=items,
        )

        assert result["items"] == items

    def test_shipment_creation_failure_publishes_failed_event(self, aws_mock, mocker):
        """If storing the shipment fails, ShipmentCreationFailed should be published."""
        _reload_shipping_modules()
        mock_publish = mocker.patch("shipping.service._publish_event", return_value=None)
        mocker.patch(
            "shipping.service.put_shipment",
            side_effect=Exception("DynamoDB write failed"),
        )
        from shipping.service import create_shipment

        with pytest.raises(Exception, match="DynamoDB write failed"):
            create_shipment(
                order_id="ord_fail",
                email="eve@test.com",
                shipping_address={},
                items=[],
            )

        failed_calls = [
            c for c in mock_publish.call_args_list
            if c[0][0] == "ShipmentCreationFailed"
        ]
        assert len(failed_calls) == 1
