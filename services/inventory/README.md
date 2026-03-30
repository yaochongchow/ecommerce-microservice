# Inventory Service

Manages product stock levels and per-order reservations. Reacts to events from other services and publishes its own events back onto the shared EventBridge bus. It never reads another service's database — all information it needs must arrive in the event payload or be present in its own tables.

Events are delivered via an SQS queue (`inventory-service-queue`) that sits between EventBridge and the Lambda. This buffers bursts of incoming events, prevents Lambda throttling under high load, and routes failed messages to a dead-letter queue (`inventory-service-dlq`) after 3 attempts.

## Data model

**InventoryTable** — one record per product

| Attribute        | Type   | Description                                        |
| ---------------- | ------ | -------------------------------------------------- |
| `productId` (PK) | String | Matches the product ID used by the product service |
| `available`      | Number | Units currently available to reserve               |
| `reserved`       | Number | Units committed to open orders not yet shipped     |
| `updatedAt`      | String | ISO-8601 timestamp of last write                   |

`available + reserved` = total physical stock in the warehouse.

**ReservationsTable** — one record per (order, product) pair

| Attribute        | Type   | Description                                          |
| ---------------- | ------ | ---------------------------------------------------- |
| `orderId` (PK)   | String | Order that created this reservation                  |
| `productId` (SK) | String | Product being reserved                               |
| `quantity`       | Number | Units reserved for this order                        |
| `status`         | String | `RESERVED` → `FULFILLED` → `RESTOCKED` or `RELEASED` |

---

## Events consumed

### From `product-service`

#### `ProductCreated`

Initializes a new inventory record.

Publishes `InventoryInitialized` on success, `InventoryInitializationFailed` if the product already exists.

```json
{
  "source": "product-service",
  "detail-type": "ProductCreated",
  "detail": {
    "productId": "prod_001",
    "stock": 100
  }
}
```

> **Required fields:** `productId`, `stock`

#### `ProductRestocked`

Adds incoming stock to the available count. Product must already exist in inventory.

Publishes `StockReplenished` on success, `ProductRestockFailed` if the product has no inventory record.

```json
{
  "source": "product-service",
  "detail-type": "ProductRestocked",
  "detail": {
    "productId": "prod_001",
    "quantity": 50
  }
}
```

> **Required fields:** `productId`, `quantity`

---

### From `order-service`

#### `OrderCreated`

Atomically reserves stock for every item in the order. Either all items are reserved or none are (rollback on partial failure).

Publishes `InventoryReserved` on success, `InventoryReservationFailed` on failure.

```json
{
  "source": "order-service",
  "detail-type": "OrderCreated",
  "detail": {
    "orderId": "ord_123",
    "items": [
      { "productId": "prod_001", "quantity": 2 },
      { "productId": "prod_002", "quantity": 1 }
    ]
  }
}
```

> **Required fields:** `orderId`, `items[]` — each item must have `productId` and `quantity`.

#### `OrderCanceled`

Releases reserved stock back to available for items still in `RESERVED` status. Items already shipped (`FULFILLED`) are not restored here — those come back via `OrderReturned` when physically returned.

Always publishes `InventoryReleased`. If no reservations were found, the event includes `"reason": "no reservations found"` so consumers can distinguish a normal release from a no-op.

```json
{
  "source": "order-service",
  "detail-type": "OrderCanceled",
  "detail": {
    "orderId": "ord_123",
    "email": "customer@example.com",
    "reason": "cancelled by customer"
  }
}
```

> **Required fields:** `orderId`.
> `email` and `reason` are not used by the inventory service but must be present for the notification service to send a cancellation email to the customer.
> Quantities are looked up from the ReservationsTable — no item list needed in this event.

#### `OrderReturned`

Restocks items that have been physically returned. Only processes items with `FULFILLED` status. Supports partial returns — only the quantities listed in `items[]` are restocked. If `items` is omitted, the full originally-reserved quantity is restored for every item in the order.

Always publishes `InventoryRestocked`. If no reservations were found, the event includes `"reason": "no reservations found"` so consumers can distinguish a real restock from a no-op.

```json
{
  "source": "order-service",
  "detail-type": "OrderReturned",
  "detail": {
    "orderId": "ord_123",
    "returnId": "ret_001",
    "items": [{ "productId": "prod_001", "quantity": 1 }]
  }
}
```

> **Required fields:** `orderId`, `returnId`.
> `items[]` is optional but strongly recommended for partial return accuracy.

---

### From `shipping-service`

#### `ShipmentCreated`

Marks reservations as `FULFILLED` and clears the reserved counter. Signals that items have physically left the warehouse.

Publishes `InventoryFulfilled` on success, `InventoryFulfillmentFailed` if no reservations are found.

```json
{
  "source": "shipping-service",
  "detail-type": "ShipmentCreated",
  "detail": {
    "shipmentId": "shp_abc123",
    "orderId": "ord_123"
  }
}
```

> **Required fields:** `orderId`.
> Quantities are looked up from the ReservationsTable — no item list needed.

---

## Events published

All events are published to the shared EventBridge bus with `source: "inventory-service"`.

### `InventoryInitialized`

A new product's inventory record has been created successfully.

```json
{ "productId": "prod_001", "available": 100 }
```

**Consumers:** product-service (confirm inventory is ready before accepting orders)

---

### `InventoryInitializationFailed`

A `ProductCreated` event arrived for a product that already has an inventory record.

```json
{ "productId": "prod_001", "reason": "product already exists in inventory" }
```

**Consumers:** product-service (investigate duplicate ProductCreated event)

---

### `StockReplenished`

Stock was successfully added to a product's available count.

```json
{ "productId": "prod_001", "quantity": 50 }
```

**Consumers:** product-service (confirm restock was applied)

---

### `ProductRestockFailed`

A `ProductRestocked` event arrived for a product with no inventory record.

```json
{ "productId": "prod_001", "reason": "product not found in inventory" }
```

**Consumers:** product-service (send `ProductCreated` first, then retry restock)

---

### `InventoryReserved`

All items in an order were successfully reserved.

```json
{
  "orderId": "ord_123",
  "items": [{ "productId": "prod_001", "quantity": 2 }]
}
```

**Consumers:** order-service (proceed to payment)

---

### `InventoryReservationFailed`

Reservation could not be completed — product not found or insufficient stock.

```json
{
  "orderId": "ord_123",
  "productId": "prod_001",
  "reason": "insufficient stock"
}
```

**`reason` values:** `"insufficient stock"`, `"product not found"`

**Consumers:** order-service (cancel the order and notify the customer)

---

### `InventoryReleased`

Reservations for an order have been released back to available stock. Always published on `OrderCanceled`.

```json
{ "orderId": "ord_123" }
```

If no reservations existed, a `reason` field is included:

```json
{ "orderId": "ord_123", "reason": "no reservations found" }
```

**Consumers:** order-service (confirm cancellation is fully processed)

---

### `InventoryFulfilled`

Reservations for an order have been marked fulfilled — items have left the warehouse.

```json
{ "orderId": "ord_123" }
```

**Consumers:** order-service or shipping-service (confirm inventory is cleared for this shipment)

---

### `InventoryFulfillmentFailed`

A `ShipmentCreated` event arrived for an order with no reservation records.

```json
{ "orderId": "ord_123", "reason": "no reservations found" }
```

**Consumers:** order-service or shipping-service (investigate missing reservation state)

---

### `InventoryRestocked`

Items from a return have been added back to available stock. Always published on `OrderReturned`.

```json
{ "orderId": "ord_123", "returnId": "ret_001" }
```

If no reservations existed, a `reason` field is included:

```json
{
  "orderId": "ord_123",
  "returnId": "ret_001",
  "reason": "no reservations found"
}
```

**Consumers:** order-service (confirm return is fully processed)

---

### `LowStock`

Available stock for a product has dropped to or below the configured threshold (default: 10 units) after a reservation.

```json
{ "productId": "prod_001", "available": 4 }
```

**Consumers:** product-service (trigger restocking workflow)

---

### `OutOfStock`

Available stock has reached zero after a reservation.

```json
{ "productId": "prod_001", "available": 0 }
```

**Consumers:** product-service (mark product unavailable), order-service (stop accepting new orders for this product)

---

## Contract requirements from other services

### order-service

- Must emit `OrderCreated` with `items[]` containing `productId` and `quantity` for every item.
- Must emit `OrderCanceled` when an order is cancelled for any reason — including payment failure or reservation expiry. The inventory service does not track payment deadlines; the order service owns that logic. Must include `email` and `reason` in the payload so the notification service can send a cancellation confirmation to the customer with context on why it was cancelled.
- Must emit `OrderReturned` with `items[]` listing the products and quantities being returned. Omitting `items[]` causes the full originally-reserved quantity to be restocked for all products in the order.
- Must listen for `InventoryReservationFailed` and cancel the order immediately.
- Should listen for `LowStock` / `OutOfStock` to stop accepting new orders for affected products.

### product-service

- Must emit `ProductCreated` before any order for that product can be placed. Reserving a product that has no inventory record will fail.
- Must emit `ProductRestocked` to add new incoming stock. Inventory will never go negative — all stock must arrive via this event.
- Should listen for `ProductRestockFailed` and ensure `ProductCreated` is sent before retrying.
- Should listen for `LowStock` / `OutOfStock` to trigger warehouse restocking workflows.

### shipping-service

- Must emit `ShipmentCreated` with `orderId` after a shipment record is created. This transitions reservations from `RESERVED` to `FULFILLED` and clears the reserved counter. If this event is never emitted, the reserved count is never cleared.

---

## Idempotency guarantees

All event handlers are safe to retry:

| Event             | Idempotency mechanism                                                         |
| ----------------- | ----------------------------------------------------------------------------- |
| `ProductCreated`  | Conditional PUT rejects duplicates; publishes `InventoryInitializationFailed` |
| `OrderCreated`    | TransactWrite reservation condition detects duplicates — no double-deduction  |
| `OrderCanceled`   | Status check skips already-released items                                     |
| `OrderReturned`   | Status check skips non-FULFILLED items                                        |
| `ShipmentCreated` | Status check skips already-fulfilled items                                    |

---

## Known limitations

- **Rollback on multi-item reservation failure is not crash-safe.** If Lambda crashes during rollback, the next retry will detect the already-reserved duplicate and return early without completing rollback. The window for this failure is very small in practice.
- **Low stock alerts fire on every qualifying reservation**, not once per threshold crossing. Downstream consumers should deduplicate.
