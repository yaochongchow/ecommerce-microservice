"""Tests for the shipping service."""

import importlib

import pytest


class TestCreateShipment:
    def test_create_shipment_success(self, aws_mock, mocker):
        """OrderConfirmed event should create a shipment with tracking number."""
        import repository; importlib.reload(repository)
        import service; importlib.reload(service)

        mock_publish = mocker.patch("service._publish_event", return_value=None)
        from service import create_shipment

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
        import repository; importlib.reload(repository)
        import service; importlib.reload(service)

        mock_publish = mocker.patch("service._publish_event", return_value=None)
        from service import create_shipment

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
        import repository; importlib.reload(repository)
        import service; importlib.reload(service)

        mocker.patch("service._publish_event", return_value=None)
        from service import create_shipment
        from repository import get_shipment_by_order

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
        import repository; importlib.reload(repository)
        import service; importlib.reload(service)

        mocker.patch("service._publish_event", return_value=None)
        from service import create_shipment

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
        import repository; importlib.reload(repository)
        import service; importlib.reload(service)

        mock_publish = mocker.patch("service._publish_event", return_value=None)
        mocker.patch(
            "service.put_shipment",
            side_effect=Exception("DynamoDB write failed"),
        )
        from service import create_shipment

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
