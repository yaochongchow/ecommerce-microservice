# Payment Service

Processes customer payments via Stripe with **idempotency protection** and **circuit breaker** resilience. Entirely event-driven — no API Gateway routes.

## DynamoDB Tables

| Table | Partition Key | GSI | Description |
|-------|--------------|-----|-------------|
| `PaymentsTable` | `payment_id` | `order_id-index` | Payment records (charge ID, amount, status, refund info) |
| `IdempotencyKeysTable` | `idempotency_key` | — | Prevents duplicate charges; 24-hour TTL auto-cleanup |

## EventBridge Events

### Published (source: `payment-service`)

| Event | When | Consumed By |
|-------|------|-------------|
| `PaymentSucceeded` | Stripe charge succeeds | Order (confirm order) |
| `PaymentFailed` | Stripe charge declined or circuit breaker open | Order (start compensation) |
| `PaymentRefunded` | Refund issued | Order (finalize cancellation) |

### Consumed

| Event | Source | Action |
|-------|--------|--------|
| `OrderReadyForPayment` | order-service | Charge customer via Stripe |
| `CompensatePayment` | order-service | Refund previous charge |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `PAYMENTS_TABLE` | DynamoDB payments table name |
| `IDEMPOTENCY_TABLE` | DynamoDB idempotency keys table name |
| `STRIPE_SECRET_KEY` | Stripe API secret key |
| `EVENT_BUS_NAME` | EventBridge bus name |

## Key Design Patterns

### Idempotency (Double Protection)

1. **DynamoDB** — Before charging, checks `IdempotencyKeysTable`. If key exists, returns cached result (no charge).
2. **Stripe** — Passes idempotency key to Stripe API as a second layer of protection.
3. **TTL** — Idempotency records auto-expire after 24 hours.

### Circuit Breaker

Protects against Stripe outages. Per-Lambda-container state machine:

```
CLOSED (normal) --[5 consecutive failures]--> OPEN (failing)
OPEN --[30s timeout]--> HALF_OPEN (testing)
HALF_OPEN --[success]--> CLOSED
HALF_OPEN --[failure]--> OPEN
```

When OPEN, immediately publishes `PaymentFailed` without calling Stripe.

### Retry with Exponential Backoff

- Retries: 3 attempts (0.5s, 1s, 2s delays)
- Retryable: `RateLimitError`, `APIConnectionError`
- Non-retryable: `CardError`, `AuthenticationError`

## Files

| File | Description |
|------|-------------|
| `handler.py` | Lambda entry point: routes `OrderReadyForPayment` and `CompensatePayment` events |
| `models.py` | DynamoDB CRUD: payment creation, GSI lookup by order, refund updates |
| `idempotency.py` | Idempotency wrapper: check cache, process, store result |
| `stripe_client.py` | Stripe integration: `create_charge`, `create_refund`, circuit breaker, retry logic |
