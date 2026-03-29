# M2 — Order & Payment Microservices

Event-driven order and payment services with saga orchestration, idempotent payments, and circuit breaker protection. Built on AWS Lambda, DynamoDB, EventBridge, and Stripe.

## Architecture Overview

```
                    API Gateway (M1)
                         │
                         ▼
                ┌─────────────────┐
                │  Order Service   │
                │  (API Handler)   │
                └────────┬────────┘
                         │ POST /orders
                         ▼
                ┌─────────────────┐       EventBridge        ┌──────────────────┐
                │  Saga Engine     │──── order.created ──────▶│ Payment Service   │
                │  (State Machine) │                          │ (Event Handler)   │
                │                  │◀── payment.completed ────│                   │
                │                  │◀── payment.failed ───────│                   │
                │                  │                          └──────────────────┘
                │                  │       EventBridge               │
                │                  │──── order.payment_completed ──▶ │ M4 Inventory
                │                  │◀── inventory.reserved ─────────│
                │                  │◀── inventory.failed ───────────│
                │                  │                                │
                │  (Compensation)  │──── saga.compensate_payment ──▶│ Payment refund
                │                  │◀── payment.refunded ───────────│
                └─────────────────┘
                         │
                         ▼
               order.confirmed / order.cancelled
                    (M4 consumes)
```

### Services

| Service | Type | Responsibility |
|---------|------|---------------|
| **Order Service (API)** | Synchronous (API Gateway → Lambda) | Handles HTTP requests: create, get, and cancel orders |
| **Order Service (Events)** | Asynchronous (EventBridge → Lambda) | Processes payment/inventory result events, drives the saga |
| **Payment Service** | Asynchronous (EventBridge → Lambda) | Charges/refunds via Stripe with idempotency protection |

### DynamoDB Tables

| Table | Partition Key | Purpose |
|-------|-------------|---------|
| `orders` | `order_id` | Order records (items, status, totals, shipping address) |
| `saga-state` | `order_id` | Saga step tracking and transition history |
| `payments` | `payment_id` (GSI: `order_id`) | Payment records (charge ID, amount, refund info) |
| `idempotency-keys` | `idempotency_key` (TTL: 24h) | Prevents duplicate Stripe charges on event retries |

---

## Project Structure

```
.
├── template.yaml              # SAM template — all AWS resources
├── requirements.txt           # Python dependencies
├── shared/                    # Shared utilities (deployed as Lambda Layer)
│   ├── __init__.py
│   ├── events.py              # EventBridge publisher + event schema builders
│   ├── logger.py              # Structured JSON logger with correlation IDs
│   └── exceptions.py          # Custom exception hierarchy
├── order_service/             # Order microservice
│   ├── __init__.py
│   ├── handler.py             # Lambda handlers (api_handler + event_handler)
│   ├── models.py              # DynamoDB CRUD for orders + saga state
│   ├── saga.py                # Saga orchestrator — state transitions + event publishing
│   └── compensation.py        # Rollback handlers (refund payment on inventory failure)
├── payment_service/           # Payment microservice
│   ├── __init__.py
│   ├── handler.py             # Lambda handler (event_handler for charges/refunds)
│   ├── models.py              # DynamoDB CRUD for payments + idempotency keys
│   ├── idempotency.py         # Idempotency layer — prevents duplicate charges
│   └── stripe_client.py       # Stripe SDK wrapper with retry + circuit breaker
└── tests/                     # Unit tests
    ├── conftest.py            # Shared fixtures (mocked AWS via moto)
    ├── test_order_handler.py  # Order API handler tests
    ├── test_payment_handler.py# Payment event handler tests
    └── test_saga.py           # Saga state machine tests
```

---

## Key Concepts

### 1. Saga Pattern (Orchestrator)

The saga manages a distributed transaction across order → payment → inventory without a shared database. The order service acts as the **orchestrator**.

**Happy path:**
```
PENDING → PAYMENT_PROCESSING → PAYMENT_COMPLETED → INVENTORY_RESERVING → CONFIRMED
```

**Payment failure (no compensation needed):**
```
PENDING → PAYMENT_PROCESSING → PAYMENT_FAILED → CANCELLED
```

**Inventory failure (compensation required):**
```
PENDING → PAYMENT_PROCESSING → PAYMENT_COMPLETED → INVENTORY_RESERVING
    → INVENTORY_FAILED → COMPENSATING → CANCELLED
```

When inventory fails after payment succeeds, the saga publishes a `saga.compensate_payment` event. The payment service listens, refunds the Stripe charge, and publishes `payment.refunded`. The order service then finalizes the cancellation.

**Why a saga instead of a distributed transaction?**
In microservices, there's no shared database, so two-phase commit isn't possible. The saga breaks the transaction into steps, each with a compensating action. If step N fails, steps N-1, N-2, ... are undone in reverse.

### 2. Idempotent Payments

EventBridge guarantees **at-least-once** delivery, meaning the payment service may receive the same `order.created` event multiple times. Without idempotency, we'd charge the card twice.

**Double protection:**
1. **Application-level** — Before processing, we check the idempotency key in DynamoDB. If found, return the cached result.
2. **Stripe-level** — We pass the same idempotency key to Stripe's API, which has its own built-in deduplication.

**Flow:**
```
Event arrives → Check idempotency table → Key exists? → Return cached result
                                        → Key new?   → Charge Stripe → Cache result
```

### 3. Circuit Breaker (Stripe Client)

The Stripe client uses a circuit breaker to avoid hammering a failing external API.

**States:**
- **CLOSED** (normal) — Requests go through. After 5 consecutive failures → OPEN.
- **OPEN** (rejecting) — All requests fail immediately with `CircuitBreakerOpenError`. After 30s cooldown → HALF_OPEN.
- **HALF_OPEN** (testing) — One test request allowed. Success → CLOSED. Failure → OPEN.

**Why?** If Stripe is down, retrying every Lambda invocation wastes time (Lambda bills by duration) and adds load. The circuit breaker fails fast, letting the DLQ capture events for retry later.

### 4. Correlation IDs

Every request gets a correlation ID (UUID) injected by M1's BFF Lambda. This ID propagates through:
- API Gateway headers → Order service logs
- EventBridge event metadata → Payment service logs
- All DynamoDB writes

This lets you trace a single user action across all services in CloudWatch Logs Insights:
```
fields @timestamp, message, order_id
| filter correlation_id = "abc-123-def"
| sort @timestamp asc
```

### 5. Event-Driven Communication

Services never call each other directly. All communication flows through EventBridge:

| Event | Publisher | Consumer | Trigger |
|-------|-----------|----------|---------|
| `order.created` | Order service | Payment service | Charge the card |
| `payment.completed` | Payment service | Order service | Advance saga to inventory |
| `payment.failed` | Payment service | Order service | Cancel the order |
| `order.payment_completed` | Order service | Inventory service (M4) | Reserve stock |
| `inventory.reserved` | Inventory (M4) | Order service | Confirm the order |
| `inventory.failed` | Inventory (M4) | Order service | Trigger compensation |
| `saga.compensate_payment` | Order service | Payment service | Refund the charge |
| `payment.refunded` | Payment service | Order service | Finalize cancellation |
| `order.confirmed` | Order service | Shipping + Notification (M4) | Ship + email |
| `order.cancelled` | Order service | Notification (M4) | Cancellation email |

---

## API Endpoints

### POST /orders
Create a new order and start the saga.

**Request:**
```json
{
  "user_id": "usr_abc123",
  "items": [
    {
      "product_id": "prod_001",
      "quantity": 2,
      "unit_price": 29.99
    }
  ],
  "shipping_address": {
    "street": "123 Main St",
    "city": "Boston",
    "state": "MA",
    "zip": "02101"
  }
}
```

**Response (201):**
```json
{
  "message": "Order created",
  "order": {
    "order_id": "ord_a1b2c3d4e5f6",
    "user_id": "usr_abc123",
    "status": "PENDING",
    "total_amount": "59.98",
    "items": [...],
    "created_at": "2026-03-29T12:00:00+00:00"
  }
}
```

### GET /orders/{id}
Retrieve an order by ID.

**Response (200):**
```json
{
  "order": {
    "order_id": "ord_a1b2c3d4e5f6",
    "status": "CONFIRMED",
    "payment_id": "pay_x1y2z3",
    "charge_id": "ch_stripe123",
    ...
  }
}
```

### PUT /orders/{id}/cancel
Cancel an order. If payment was already charged, triggers a refund.

**Response (200):**
```json
{
  "message": "Order cancelled",
  "order_id": "ord_a1b2c3d4e5f6"
}
```

---

## Setup & Deployment

### Prerequisites
- Python 3.12+
- AWS CLI configured with credentials
- AWS SAM CLI (`brew install aws-sam-cli`)
- Stripe account (test mode API key)

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run tests (uses moto — no AWS credentials needed)
pytest tests/ -v

# Start local API (requires Docker for DynamoDB Local)
sam local start-api
```

### Deploy to AWS

```bash
# Build the SAM application
sam build

# Deploy (interactive on first run — saves config to samconfig.toml)
sam deploy --guided

# Subsequent deploys
sam deploy
```

**Parameters to set during guided deploy:**
| Parameter | Description | Example |
|-----------|-------------|---------|
| `Environment` | Deployment environment | `dev`, `staging`, `prod` |
| `EventBusName` | EventBridge bus name (from M1) | `ecommerce-event-bus` |
| `StripeSecretKey` | Stripe API secret key | `sk_test_...` |

### Run Tests

```bash
# All tests
pytest tests/ -v

# Just order tests
pytest tests/test_order_handler.py -v

# Just saga tests
pytest tests/test_saga.py -v

# With coverage
pytest tests/ --cov=order_service --cov=payment_service --cov=shared -v
```

---

## Integration with Other Members

### What M2 depends on (from M1):
- **EventBridge bus** — M1 creates `ecommerce-event-bus`. M2 publishes and subscribes to it.
- **API Gateway** — M1 creates the main API Gateway. M2's routes (`/orders/*`) are added to it.
- **Shared libraries** — M2 includes its own shared/ layer, but M1 provides the canonical logger/tracer if available.

### What M2 provides to others:
- **Event schemas** — M4 (inventory) needs to know the shape of `order.created` and `order.payment_completed` events. These are defined in `shared/events.py`.
- **Order status** — M4 (notification) reacts to `order.confirmed` and `order.cancelled`.

### Integration timeline:
1. **Day 1-3:** Build and test locally with mocked EventBridge (moto).
2. **Day 3-4:** Wire up to M1's deployed EventBridge bus.
3. **Day 4-5:** End-to-end test with M4's inventory service.

---

## Error Handling

| Error | HTTP Code | Handling |
|-------|-----------|----------|
| Missing required fields | 400 | Validation in `api_handler` |
| Order not found | 404 | `OrderNotFoundError` exception |
| Order already cancelled | 409 | Status check in cancel handler |
| Card declined | 402 | Stripe `CardError` → `payment.failed` event |
| Stripe unavailable | 503 | Circuit breaker trips → `payment.failed` event |
| Saga compensation failure | 500 | Event goes to DLQ for manual retry |
