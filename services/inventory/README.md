# Inventory Service

Manages product stock levels with **atomic reservation/release operations**. Prevents overselling through DynamoDB transactional writes and tracks per-order reservation status through its lifecycle.

## DynamoDB Tables

| Table | Keys | Description |
|-------|------|-------------|
| `InventoryTable` | PK: `productId` | Stock levels: `available` (in-stock) and `reserved` (held for orders) |
| `ReservationsTable` | PK: `orderId`, SK: `productId` | Per-item reservation records with status tracking |

### Reservation Status Lifecycle

```
RESERVED -> FULFILLED -> (optionally) RESTOCKED
    |
    v
  RELEASED (if order cancelled before shipping)
```

## EventBridge Events

### Published (source: `inventory-service`)

| Event | When |
|-------|------|
| `InventoryInitialized` | Product inventory record created |
| `StockReplenished` | Stock added to product |
| `InventoryReserved` | All items reserved for order |
| `InventoryReservationFailed` | Insufficient stock or product not found |
| `InventoryReleased` | Reserved items released (order cancelled) |
| `InventoryFulfilled` | Items marked as shipped |
| `InventoryRestocked` | Items returned and restocked |
| `LowStock` | Available stock <= 10 units |
| `OutOfStock` | Available stock = 0 |

### Consumed

| Event | Source | Action |
|-------|--------|--------|
| `ProductCreated` | product-service | Initialize inventory record |
| `ProductRestocked` | product-service | Add incoming stock |
| `OrderCreated` | order-service | Reserve items (atomic transaction) |
| `OrderCanceled` | order-service | Release reserved items |
| `CompensateInventory` | order-service | Release reserved items (saga compensation) |
| `ShipmentCreated` | shipping-service | Mark items as FULFILLED |
| `OrderReturned` | order-service | Restock fulfilled items |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `INVENTORY_TABLE_NAME` | DynamoDB inventory table name |
| `RESERVATIONS_TABLE_NAME` | DynamoDB reservations table name |
| `EVENT_BUS_NAME` | EventBridge bus name |
| `LOW_STOCK_THRESHOLD` | Low stock alert threshold (default: 10) |

## Key Design Patterns

### Atomic Transactions

`transact_reserve()` uses DynamoDB `TransactWriteItems` to atomically:
1. Decrement `available` and increment `reserved` in InventoryTable
2. Create a reservation record in ReservationsTable

Both operations succeed or both fail — no partial state.

### Rollback on Partial Failure

If an order has 3 items and item #3 fails to reserve, items #1 and #2 are automatically rolled back (released). Ensures all-or-nothing semantics.

### Idempotency Guards

- Duplicate `OrderCreated` detected via condition check on reservation record
- Duplicate `ShipmentCreated` skipped if reservation already FULFILLED
- Duplicate `OrderReturned` returns early if no matching reservations exist

### Low Stock Alerts

After each reservation, checks remaining stock. Publishes `LowStock` (available <= threshold) or `OutOfStock` (available = 0) for downstream alerting.

## Files

| File | Description |
|------|-------------|
| `handler.py` | Lambda entry point: routes events by detail-type to service functions |
| `service.py` | Business logic: `reserve_inventory`, `release_inventory`, `fulfill_inventory`, `restock_inventory` |
| `repository.py` | DynamoDB operations: `transact_reserve`, `atomic_release`, `atomic_fulfill`, `get_reservations_by_order` |
| `models.py` | Data classes: `OrderItem`, `InventoryRecord` |
