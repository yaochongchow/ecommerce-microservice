# Notification Service

Sends transactional email notifications to customers. Supports both **AWS SES** (production) and **mock** (logging) modes for easy development and testing.

## Email Types

| Trigger Event | Email Type | Content |
|--------------|------------|---------|
| `OrderConfirmed` | Order Confirmation | Order ID, item list, total amount |
| `ShipmentCreated` | Shipment Notification | Carrier, tracking number, delivery estimate |
| `OrderCanceled` | Cancellation Notice | Reason, refund information |

## EventBridge Events

### Consumed

| Event | Source | Action |
|-------|--------|--------|
| `OrderConfirmed` | order-service | Send order confirmation email |
| `ShipmentCreated` | shipping-service | Send shipment tracking email |
| `OrderCanceled` | order-service | Send cancellation email |

### Published

None — this is a consume-only service.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `EMAIL_MODE` | `"ses"` for AWS SES, `"mock"` for CloudWatch logging | `"mock"` |
| `SES_FROM_EMAIL` | Sender email address (SES mode only) | `"noreply@example.com"` |

## Key Design Patterns

### Graceful Degradation

- **Mock mode** (default): Logs email subject, recipient, and body to CloudWatch. No SES setup required.
- **SES mode**: Sends real emails via AWS SES. Requires verified sender domain/email in SES.
- Switch between modes by changing `EMAIL_MODE` environment variable — no code changes needed.

### Template-Based Formatting

Each email type has a dedicated formatter that extracts relevant fields from the event payload and composes a human-readable email body with subject line.

## Files

| File | Description |
|------|-------------|
| `handler.py` | Lambda entry point: routes events to notification functions |
| `service.py` | Email formatters: `notify_payment_succeeded`, `notify_shipment_created`, `notify_order_canceled` |
| `email_client.py` | Email delivery: `send_email` router, `_send_via_ses`, `_send_mock` |
