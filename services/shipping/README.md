# Shipping Service

Creates shipment records and issues tracking information when payment is confirmed. Reacts to events from other services and publishes its own events back onto the shared EventBridge bus. It never reads another service's database — all information it needs must arrive in the event payload or be present in its own table.

Events are delivered via an SQS queue (`shipping-service-queue`) that sits between EventBridge and the Lambda. Failed messages are routed to a dead-letter queue (`shipping-service-dlq`) after 3 attempts.

## Data model

**ShipmentsTable** — one record per shipment

| Attribute         | Type   | Description                                      |
| ----------------- | ------ | ------------------------------------------------ |
| `shipmentId` (PK) | String | Unique shipment ID (`shp_<hex>`)                 |
| `orderId`         | String | Order this shipment fulfills (GSI partition key) |
| `email`           | String | Customer email for notification                  |
| `carrier`         | String | Carrier name (e.g. `UPS_MOCK`)                   |
| `trackingNumber`  | String | Carrier-issued tracking number                   |
| `status`          | String | `LABEL_CREATED` (extensible for future statuses) |
| `shippingAddress` | Map    | Delivery address                                 |
| `items`           | List   | Items included in this shipment                  |
| `createdAt`       | String | ISO-8601 timestamp                               |

**GSI: `orderId-index`** — allows lookup by `orderId` without scanning the full table. Used for idempotency checks and future order-service queries.

---

## Events consumed

### From `payment-service`

#### `PaymentSucceeded`

Triggers shipment creation. The shipping service uses the order ID, customer email, shipping address, and item list from this event to build the shipment record.

```json
{
  "source": "payment-service",
  "detail-type": "PaymentSucceeded",
  "detail": {
    "orderId": "ord_123",
    "email": "customer@example.com",
    "shippingAddress": {
      "name": "Jane Doe",
      "addressLine1": "123 Main St",
      "addressLine2": "Apt 4B",
      "city": "Boston",
      "state": "MA",
      "zip": "02101",
      "country": "US"
    },
    "items": [{ "productId": "prod_001", "quantity": 2 }]
  }
}
```

> **Required fields:** `orderId`, `email`, `shippingAddress`.
> `items` is optional but should be included for inventory fulfillment accuracy downstream.

---

## Events published

All events are published to the shared EventBridge bus with `source: "shipping-service"`.

### `ShipmentCreated`

A shipment record has been created and a tracking number assigned. Published on every successful `PaymentSucceeded` — including re-published on duplicate events (idempotent).

```json
{
  "shipmentId": "shp_abc123",
  "orderId": "ord_123",
  "email": "customer@example.com",
  "carrier": "UPS_MOCK",
  "trackingNumber": "MOCK-20260328-0042",
  "status": "LABEL_CREATED",
  "items": [{ "productId": "prod_001", "quantity": 2 }]
}
```

**Consumers:**

- `order-service` — updates order status to `SHIPPED`, stores tracking number
- `inventory-service` — transitions reservations from `RESERVED` to `FULFILLED`
- `notification-service` — sends shipment confirmation email with tracking number

---

### `ShipmentCreationFailed`

Shipment record could not be persisted. Published when an unexpected storage error occurs after confirming the order has no existing shipment.

```json
{
  "orderId": "ord_123",
  "reason": "failed to store shipment"
}
```

**Consumers:**

- `order-service` — flag the order as stuck; payment succeeded but shipment could not be created
- `payment-service` — may trigger a manual review or retry flow

---

## Contract requirements from other services

### payment-service

- Must emit `PaymentSucceeded` with `orderId`, `email`, and `shippingAddress` after a payment is confirmed.
- Should include `items[]` in the payload so downstream services (inventory, notification) have item-level detail.
- Should listen for `ShipmentCreationFailed` and decide whether to retry or escalate.

### order-service

- Should listen for `ShipmentCreated` to update order status to `SHIPPED` and store the `trackingNumber` and `shipmentId` for customer-facing tracking.
- Should listen for `ShipmentCreationFailed` to flag the order and alert operations.

### inventory-service

- Must listen for `ShipmentCreated` to transition reservations from `RESERVED` to `FULFILLED` and clear the reserved counter.

### notification-service

- Should listen for `ShipmentCreated` to send the customer a shipment confirmation email with carrier and tracking number.

---

## Idempotency guarantees

| Event              | Idempotency mechanism                                                                 |
| ------------------ | ------------------------------------------------------------------------------------- |
| `PaymentSucceeded` | `get_shipment_by_order` check before creation; republishes `ShipmentCreated` if found |

If a duplicate `PaymentSucceeded` arrives after a shipment has already been created, the service detects the existing record via the `orderId-index` GSI and republishes `ShipmentCreated` with the original shipment data — ensuring downstream consumers that may have missed the first event still receive it.

---

## Carrier integration

The current implementation uses a mock carrier (`UPS_MOCK`) with a generated tracking number. To integrate a real carrier:

1. Add a `carrier_client.py` module that calls the carrier's label creation API.
2. In `create_shipment`, replace `_generate_tracking_number()` with the carrier API call.
3. Store the returned `trackingNumber`, `carrier`, and optionally a `labelUrl` on the shipment item.
4. Pass package dimensions and weight in the `PaymentSucceeded` event (or look them up from a product catalog).

No changes are needed to the handler, repository, CDK stack, or downstream event consumers — the `ShipmentCreated` payload shape remains the same.

---

## Known limitations

- **Single shipment per order.** The idempotency check assumes one shipment per order. Split shipments (multiple packages for one order) are not supported.
- **No carrier status updates.** The shipment status stays `LABEL_CREATED` indefinitely. A real implementation would need carrier webhooks or polling to publish `ShipmentDelivered` and similar events.
- **`ShipmentCreationFailed` is not retried.** If the DynamoDB write fails, the event is published and the exception re-raised. SQS will retry the message up to 3 times before moving it to the DLQ. If all retries are exhausted the order remains in a stuck state requiring manual intervention.
