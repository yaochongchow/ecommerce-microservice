# Order Service

Orchestrates the order fulfillment workflow using the **distributed saga pattern**. Manages order creation, tracks state transitions through inventory reservation, payment processing, and confirmation, and coordinates compensation (rollback) when a step fails.

## Saga State Machine

```
Happy path:
  PENDING -> INVENTORY_RESERVING -> INVENTORY_RESERVED -> PAYMENT_PROCESSING -> CONFIRMED

Inventory failure (no compensation needed):
  PENDING -> INVENTORY_RESERVING -> INVENTORY_FAILED -> CANCELLED

Payment failure (compensation required):
  PENDING -> ... -> PAYMENT_PROCESSING -> PAYMENT_FAILED -> COMPENSATING -> CANCELLED
```

## DynamoDB Tables

| Table | Partition Key | Description |
|-------|--------------|-------------|
| `OrdersTable` | `order_id` | Order records (items, totals, status, shipping address) |
| `SagaStateTable` | `order_id` | Saga state machine (current state, history log, reservation/payment IDs) |

## API Routes

| Method | Path | Description |
|--------|------|-------------|
| POST | `/orders` | Create order (validates items, starts saga) |
| GET | `/orders/{id}` | Fetch order by ID |
| PUT | `/orders/{id}/cancel` | Cancel order (triggers compensation if inventory reserved) |

### Create Order Request

```json
{
  "user_id": "usr_abc123",
  "items": [
    { "product_id": "prod_001", "quantity": 2, "unit_price": 29.99 }
  ],
  "shipping_address": { "street": "123 Main St", "city": "Boston", "state": "MA", "zip": "02101" }
}
```

## EventBridge Events

### Published (source: `order-service`)

| Event | When | Consumed By |
|-------|------|-------------|
| `OrderCreated` | Order created, saga starts | Inventory (reserve stock) |
| `OrderReadyForPayment` | Inventory reserved | Payment (charge card) |
| `OrderConfirmed` | Payment succeeded | Shipping, Notification |
| `OrderCanceled` | Failure or user cancel | Inventory (release), Notification |
| `CompensateInventory` | Payment fails after reservation | Inventory (release reserved stock) |

### Consumed

| Event | Source | Action |
|-------|--------|--------|
| `InventoryReserved` | inventory-service | Advance saga to payment step |
| `InventoryReservationFailed` | inventory-service | Cancel order (no compensation) |
| `PaymentSucceeded` | payment-service | Confirm order, publish OrderConfirmed |
| `PaymentFailed` | payment-service | Start compensation flow |
| `InventoryReleased` | inventory-service | Finalize cancellation |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ORDERS_TABLE` | DynamoDB orders table name |
| `SAGA_STATE_TABLE` | DynamoDB saga state table name |
| `EVENT_BUS_NAME` | EventBridge bus name |

## Key Design Patterns

- **Distributed Saga** — Orchestrates multi-service transactions without 2PC. Each step publishes an event and waits for a response.
- **Optimistic Locking** — `transition_saga_state()` uses DynamoDB condition expressions to prevent concurrent state transitions.
- **Compensation** — When payment fails after inventory is reserved, publishes `CompensateInventory` to release stock before cancelling.
- **Idempotency Key** — Generated per order and propagated through the event chain to prevent duplicate processing on retries.

## Files

| File | Description |
|------|-------------|
| `handler.py` | Lambda entry points: `api_handler` (HTTP) and `event_handler` (EventBridge via SQS) |
| `saga.py` | Saga orchestrator: `start_saga`, `handle_inventory_reserved`, `handle_payment_completed`, etc. |
| `compensation.py` | Compensation handlers: `compensate_inventory`, `handle_inventory_released` |
| `models.py` | DynamoDB CRUD: order creation, saga state transitions, status updates |
