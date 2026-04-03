import os
import json
import boto3
from datetime import datetime, timezone
from typing import List

from repository import (
    put_product, add_stock, get_inventory,
    transact_reserve, atomic_release, atomic_fulfill, delete_reservation,
    get_reservations_by_order, update_reservation_status,
)
from common.logger import get_logger

logger = get_logger(__name__)

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")
LOW_STOCK_THRESHOLD = int(os.environ.get("LOW_STOCK_THRESHOLD", "10"))
events_client = boto3.client("events")

# Reservation status values
RESERVED   = "RESERVED"
FULFILLED  = "FULFILLED"
RELEASED   = "RELEASED"
RESTOCKED  = "RESTOCKED"


def create_product(product_id: str, stock: int):
    """On ProductCreated: initialize inventory record with given stock, reserved=0.
    Publishes InventoryInitialized on success, InventoryInitializationFailed on duplicate.
    """
    logger.info(f"Creating inventory record for product {product_id} with stock={stock}")
    try:
        now = datetime.now(timezone.utc).isoformat()
        put_product(product_id, stock, now)
        _publish_event("InventoryInitialized", {"productId": product_id, "available": stock})
        logger.info(f"Inventory record created for {product_id}")
    except Exception as e:
        if "ConditionalCheckFailedException" in type(e).__name__:
            _publish_event("InventoryInitializationFailed", {
                "productId": product_id,
                "reason": "product already exists in inventory",
            })
            raise ValueError(f"Product {product_id} already exists in inventory")
        raise


def restock_product(product_id: str, quantity: int):
    """On ProductRestocked: add incoming stock to available. Product must already exist.
    Publishes ProductRestockFailed if the product has no inventory record.
    """
    logger.info(f"Restocking product {product_id} with quantity={quantity}")
    try:
        now = datetime.now(timezone.utc).isoformat()
        add_stock(product_id, quantity, now)
        _publish_event("StockReplenished", {"productId": product_id, "quantity": quantity})
        logger.info(f"Stock updated for {product_id}: +{quantity}")
    except Exception as e:
        if "ConditionalCheckFailedException" in type(e).__name__:
            _publish_event("ProductRestockFailed", {
                "productId": product_id,
                "quantity": quantity,
                "reason": "product not found in inventory",
            })
            raise ValueError(f"Product {product_id} not found in inventory")
        raise


def reserve_inventory(order_id: str, items: List[dict]):
    """On OrderCreated: atomically reserve stock for each item.

    Each item's check and decrement are a single DynamoDB operation so concurrent
    orders cannot both pass on the same units (no overselling).

    If any item fails mid-way, already-committed items are rolled back so the
    order is fully reserved or not at all.
    """
    logger.info(f"Reserving inventory for order {order_id}")

    # Read upfront only for product-not-found check — stock enforcement is at write time
    items_to_reserve = []
    for item in items:
        product_id = str(item["productId"])
        quantity = int(item["quantity"])
        if get_inventory(product_id) is None:
            _publish_reservation_failed(order_id, product_id, "product not found")
            raise ValueError(f"Product {product_id} not found in inventory")
        items_to_reserve.append((product_id, quantity))

    now = datetime.now(timezone.utc).isoformat()
    committed = []  # track successful writes for rollback on partial failure

    for product_id, quantity in items_to_reserve:
        try:
            transact_reserve(order_id, product_id, quantity, now)
        except Exception as e:
            if "TransactionCanceledException" in type(e).__name__:
                reasons = getattr(e, "response", {}).get("CancellationReasons", [])
                reason0 = reasons[0].get("Code", "") if len(reasons) > 0 else ""
                reason1 = reasons[1].get("Code", "") if len(reasons) > 1 else ""

                if reason1 == "ConditionalCheckFailed":
                    # Reservation record already exists — duplicate event, already processed
                    logger.warning(f"Duplicate OrderCreated for order {order_id}, product {product_id} — skipping")
                    return

                if reason0 == "ConditionalCheckFailed":
                    # Insufficient stock or product missing
                    logger.warning(f"Insufficient stock for {product_id}, rolling back {len(committed)} committed item(s)")
                    _rollback_reserve(order_id, committed, now)
                    _publish_reservation_failed(order_id, product_id, "insufficient stock")
                    raise ValueError(f"Insufficient stock for {product_id}")

            raise

        committed.append((product_id, quantity))

    _publish_event("InventoryReserved", {"orderId": order_id, "items": items})
    logger.info(f"Inventory reserved for order {order_id}")

    _check_low_stock(items_to_reserve)


def _rollback_reserve(order_id: str, committed: list, now: str):
    """Undo already-committed atomic reserves when a later item in the same order fails."""
    for product_id, quantity in committed:
        try:
            atomic_release(product_id, quantity, now)
            delete_reservation(order_id, product_id)
            logger.info(f"Rolled back reservation for {product_id} on order {order_id}")
        except Exception as e:
            logger.error(f"Rollback failed for {product_id} on order {order_id}: {e}")


def _check_low_stock(items_to_reserve: list):
    """After a successful reservation, read each product and publish LowStock or OutOfStock
    if available has dropped to or below the threshold. Fires per-product, best-effort.
    """
    for product_id, _ in items_to_reserve:
        try:
            record = get_inventory(product_id)
            if record is None:
                continue
            available = int(record["available"])
            if available == 0:
                _publish_event("OutOfStock", {"productId": product_id, "available": 0})
                logger.warning(f"OutOfStock: {product_id}")
            elif available <= LOW_STOCK_THRESHOLD:
                _publish_event("LowStock", {"productId": product_id, "available": available})
                logger.warning(f"LowStock: {product_id}, available={available}")
        except Exception as e:
            logger.error(f"Low-stock check failed for {product_id}: {e}")


def _publish_reservation_failed(order_id: str, product_id: str, reason: str):
    _publish_event("InventoryReservationFailed", {
        "orderId": order_id,
        "productId": product_id,
        "reason": reason,
    })


def release_inventory(order_id: str):
    """On OrderCanceled: use reservation records as source of truth.
    - RESERVED  → items still in warehouse, restore available
    - FULFILLED → items already shipped, do not restore (wait for OrderReturned)
    """
    logger.info(f"Releasing inventory for order {order_id}")

    reservations = get_reservations_by_order(order_id)
    if not reservations:
        logger.warning(f"No reservations found for order {order_id}, nothing to release")
        _publish_event("InventoryReleased", {"orderId": order_id, "reason": "no reservations found"})
        return

    now = datetime.now(timezone.utc).isoformat()
    for res in reservations:
        product_id = res["productId"]
        quantity = int(res["quantity"])
        status = res["status"]

        if status == RESERVED:
            atomic_release(product_id, quantity, now)
            update_reservation_status(order_id, product_id, RELEASED)

        elif status == FULFILLED:
            # Items already left the warehouse — do not restore available and do not
            # change the status. Keeping it FULFILLED lets OrderReturned correctly
            # restock when items physically come back.
            logger.info(
                f"Product {product_id} already shipped for order {order_id}; "
                f"skipping available restore — expect OrderReturned when items come back"
            )

        else:
            logger.warning(f"Unexpected reservation status '{status}' for {product_id} on order {order_id}")

    _publish_event("InventoryReleased", {"orderId": order_id})
    logger.info(f"Inventory release complete for order {order_id}")


def fulfill_inventory(order_id: str):
    """On ShipmentCreated: mark reservations as FULFILLED and clear reserved counter.
    Items have physically left the warehouse.
    """
    logger.info(f"Fulfilling inventory for order {order_id}")

    reservations = get_reservations_by_order(order_id)
    if not reservations:
        logger.warning(f"No reservations found for order {order_id}, nothing to fulfill")
        _publish_event("InventoryFulfillmentFailed", {
            "orderId": order_id,
            "reason": "no reservations found",
        })
        return

    now = datetime.now(timezone.utc).isoformat()
    for res in reservations:
        product_id = res["productId"]
        quantity = int(res["quantity"])
        status = res["status"]

        if status != RESERVED:
            # Idempotency guard: already FULFILLED (duplicate ShipmentCreated) or in another terminal state — skip
            logger.info(f"Skipping fulfill for {product_id} on order {order_id}: status is '{status}'")
            continue

        atomic_fulfill(product_id, quantity, now)
        update_reservation_status(order_id, product_id, FULFILLED)

    _publish_event("InventoryFulfilled", {"orderId": order_id})
    logger.info(f"Inventory fulfilled for order {order_id}")


def restock_inventory(order_id: str, return_id: str, items: List[dict]):
    """On OrderReturned: restock only the quantities actually returned (partial return support).
    Only processes items that are FULFILLED (physically returned from shipment).
    Uses atomic ADD to avoid read-then-write race conditions.
    """
    logger.info(f"Restocking inventory for return {return_id} on order {order_id}")

    reservations = get_reservations_by_order(order_id)
    if not reservations:
        logger.warning(f"No reservations found for order {order_id}, nothing to restock")
        _publish_event("InventoryRestocked", {"orderId": order_id, "returnId": return_id, "reason": "no reservations found"})
        return

    # Build a lookup of returned quantities from the event.
    # If items list is empty (event didn't include it), fall back to full reservation quantity.
    returned_qty = {str(item["productId"]): int(item["quantity"]) for item in items}

    now = datetime.now(timezone.utc).isoformat()
    for res in reservations:
        product_id = res["productId"]
        reserved_quantity = int(res["quantity"])
        status = res["status"]

        if status == FULFILLED:
            # Use returned quantity from event; cap at reserved quantity to be safe.
            # If product not in returned list, skip (not part of this return).
            if returned_qty and product_id not in returned_qty:
                logger.info(f"Product {product_id} not in return items list, skipping")
                continue
            quantity = min(returned_qty.get(product_id, reserved_quantity), reserved_quantity)

            # Atomic ADD — no read-then-write race condition
            add_stock(product_id, quantity, now)
            update_reservation_status(order_id, product_id, RESTOCKED)
            logger.info(f"Restocked {quantity} of {product_id} for return {return_id} (reserved was {reserved_quantity})")

        else:
            logger.warning(
                f"Product {product_id} has status '{status}' (expected FULFILLED) "
                f"for return {return_id} — skipping"
            )

    _publish_event("InventoryRestocked", {"orderId": order_id, "returnId": return_id})
    logger.info(f"Inventory restocked for return {return_id}")


def _publish_event(detail_type: str, detail: dict):
    try:
        events_client.put_events(Entries=[{
            "Source": "inventory-service",
            "DetailType": detail_type,
            "Detail": json.dumps(detail),
            "EventBusName": EVENT_BUS_NAME,
        }])
        logger.info(f"Published event: {detail_type}")
    except Exception as e:
        logger.error(f"Failed to publish event {detail_type}: {e}")
