"""
EventBridge event publisher and schema definitions.

All microservices communicate via a shared EventBridge bus.
Events use PascalCase detail-types and camelCase field names.

Event sources:
    order-service     — OrderCreated, OrderReadyForPayment, OrderConfirmed, OrderCanceled
    payment-service   — PaymentSucceeded, PaymentFailed, PaymentRefunded
    inventory-service — InventoryReserved, InventoryReservationFailed, InventoryReleased
    shipping-service  — ShipmentCreated
"""

import json
import os
from datetime import datetime, timezone

import boto3

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "ecommerce-event-bus")

_eventbridge_client = None


def _get_client():
    global _eventbridge_client
    if _eventbridge_client is None:
        _eventbridge_client = boto3.client("events")
    return _eventbridge_client


def publish_event(detail_type, detail, source, correlation_id=None):
    payload = {**detail}
    if correlation_id:
        payload["correlationId"] = correlation_id
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()

    response = _get_client().put_events(
        Entries=[
            {
                "Source": source,
                "DetailType": detail_type,
                "Detail": json.dumps(payload, default=str),
                "EventBusName": EVENT_BUS_NAME,
            }
        ]
    )
    return response


# ── Event builders (camelCase fields to match M4) ─────────────────────────


def build_order_created_event(order_id, user_id, items, total_amount, currency="USD", idempotency_key=None, shipping_address=None):
    return {
        "orderId": order_id,
        "userId": user_id,
        "items": [
            {"productId": i.get("product_id", i.get("productId", "")),
             "quantity": i["quantity"],
             "unitPrice": float(i.get("unit_price", i.get("unitPrice", 0)))}
            for i in items
        ],
        "totalAmount": float(total_amount),
        "currency": currency,
        "idempotencyKey": idempotency_key,
        "shippingAddress": shipping_address or {},
    }


def build_order_ready_for_payment_event(order_id, user_id, items, total_amount, currency="USD", idempotency_key=None):
    return {
        "orderId": order_id,
        "userId": user_id,
        "items": [
            {"productId": i.get("product_id", i.get("productId", "")),
             "quantity": i["quantity"],
             "unitPrice": float(i.get("unit_price", i.get("unitPrice", 0)))}
            for i in items
        ],
        "totalAmount": float(total_amount),
        "currency": currency,
        "idempotencyKey": idempotency_key,
    }


def build_payment_completed_event(order_id, payment_id, charge_id, amount, currency="USD"):
    return {
        "orderId": order_id,
        "paymentId": payment_id,
        "chargeId": charge_id,
        "amount": float(amount),
        "currency": currency,
    }


def build_payment_failed_event(order_id, reason, error_code=None):
    return {
        "orderId": order_id,
        "reason": reason,
        "errorCode": error_code,
    }


def build_saga_compensate_inventory_event(order_id, reservation_id, reason):
    return {
        "orderId": order_id,
        "reservationId": reservation_id,
        "reason": reason,
    }


def build_order_confirmed_event(order_id, user_id, items, total_amount, shipping_address=None, email=None):
    return {
        "orderId": order_id,
        "userId": user_id,
        "email": email or "customer@example.com",
        "items": [
            {"productId": i.get("product_id", i.get("productId", "")),
             "quantity": i["quantity"],
             "unitPrice": float(i.get("unit_price", i.get("unitPrice", 0)))}
            for i in items
        ],
        "totalAmount": float(total_amount),
        "shippingAddress": shipping_address or {},
    }


def build_order_cancelled_event(order_id, user_id, reason, email=None):
    return {
        "orderId": order_id,
        "userId": user_id,
        "email": email or "customer@example.com",
        "reason": reason,
    }
