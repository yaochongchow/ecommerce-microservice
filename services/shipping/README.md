# Shipping Service

Creates shipment records when orders are confirmed. Generates mock tracking numbers and publishes events to trigger inventory fulfillment and customer notification.

## DynamoDB Tables

| Table | Partition Key | GSI | Description |
|-------|--------------|-----|-------------|
| `ShipmentsTable` | `shipmentId` | `orderId-index` | Shipment records with tracking info |

## EventBridge Events

### Published (source: `shipping-service`)

| Event | When | Consumed By |
|-------|------|-------------|
| `ShipmentCreated` | Shipment record created | Inventory (fulfill stock), Notification (tracking email) |
| `ShipmentCreationFailed` | Storage error | DLQ for manual review |

### Consumed

| Event | Source | Action |
|-------|--------|--------|
| `OrderConfirmed` | order-service | Create shipment with tracking number |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SHIPMENTS_TABLE_NAME` | DynamoDB shipments table name |
| `EVENT_BUS_NAME` | EventBridge bus name |

## Key Design Patterns

### Idempotency

On duplicate `OrderConfirmed` events, checks if a shipment already exists for the order via the `orderId-index` GSI. If found, republishes the cached `ShipmentCreated` event without creating a new record.

### Mock Tracking Numbers

Format: `MOCK-YYYYMMDD-XXXX` (e.g., `MOCK-20260329-A7F2`). Simulates real carrier integration without external API calls. Replace with ShipStation/UPS/FedEx integration for production.

## Files

| File | Description |
|------|-------------|
| `handler.py` | Lambda entry point: handles `OrderConfirmed` events via SQS |
| `service.py` | Business logic: `create_shipment`, tracking number generation, event publishing |
| `repository.py` | DynamoDB operations: `put_shipment`, `get_shipment_by_order` (GSI lookup) |
| `models.py` | Data classes: `ShippingAddress`, `Shipment` |
