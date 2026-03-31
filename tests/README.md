# Tests

All tests are organized by service under `tests/`. Python services use **pytest** with **moto** for AWS mocking. CDK stacks use **Jest** with CDK assertions.

## Directory Structure

```
tests/
├── conftest.py              # Shared fixtures (DynamoDB tables, EventBridge, Lambda context)
├── order/                   # Order service tests (saga, API handler, DynamoDB models)
│   ├── test_handler.py      # API handler: POST/GET/PUT /orders
│   ├── test_saga.py         # Saga state machine transitions
│   └── test_models.py       # DynamoDB table operations
├── payment/                 # Payment service tests
│   └── test_handler.py      # Payment processing, idempotency, refunds
├── inventory/               # Inventory service tests
│   └── test_service.py      # Stock reservation, release, fulfillment, alerts
├── shipping/                # Shipping service tests
│   └── test_service.py      # Shipment creation, idempotency
├── notification/            # Notification service tests
│   └── test_service.py      # Email formatting, mock/SES modes
└── cdk/                     # CDK infrastructure tests (TypeScript)
    └── stacks.test.ts       # Stack resource assertions
```

## Prerequisites

### Python Tests

```bash
pip install pytest pytest-mock moto boto3 stripe
```

Or install from requirements:

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### CDK Tests (TypeScript)

```bash
npm install
```

## Running Tests

### Run All Python Tests

```bash
pytest tests/ -v
```

### Run Tests by Service

```bash
# Order service
pytest tests/order/ -v

# Payment service
pytest tests/payment/ -v

# Inventory service
pytest tests/inventory/ -v

# Shipping service
pytest tests/shipping/ -v

# Notification service
pytest tests/notification/ -v
```

### Run a Single Test File

```bash
pytest tests/order/test_saga.py -v
```

### Run a Single Test

```bash
pytest tests/order/test_saga.py::TestSagaHappyPath::test_start_saga -v
```

### Run CDK Stack Tests

```bash
npx jest
```

Or run specific stack tests:

```bash
npx jest --testPathPattern=stacks
```

### Run All Tests (Python + CDK)

```bash
pytest tests/ -v && npx jest
```

## Test Details

### Order Tests (`tests/order/`)

| File | Tests | What It Covers |
|------|-------|----------------|
| `test_handler.py` | 7 | POST /orders (success, validation errors), GET /orders/{id} (success, 404), PUT /orders/{id}/cancel, routing |
| `test_saga.py` | 5 | Happy path (PENDING → CONFIRMED), inventory failure → CANCELLED, payment failure → compensation, state history |
| `test_models.py` | 8 | DynamoDB record structure, saga state storage, payment record creation, cross-service table queries |

### Payment Tests (`tests/payment/`)

| File | Tests | What It Covers |
|------|-------|----------------|
| `test_handler.py` | 4 | Successful Stripe charge, idempotent retry (no double charge), card declined → PaymentFailed, refund flow |

### Inventory Tests (`tests/inventory/`)

| File | Tests | What It Covers |
|------|-------|----------------|
| `test_service.py` | 15 | Product creation, stock reservation (success/insufficient/not found/multi-item), low stock alerts, release, fulfillment, restock |

### Shipping Tests (`tests/shipping/`)

| File | Tests | What It Covers |
|------|-------|----------------|
| `test_service.py` | 5 | Shipment creation, mock tracking number, idempotent duplicate, DynamoDB persistence, order items |

### Notification Tests (`tests/notification/`)

| File | Tests | What It Covers |
|------|-------|----------------|
| `test_service.py` | 8 | Order confirmation email, shipment tracking email, cancellation email, mock mode logging, SES mode |

### CDK Tests (`tests/cdk/`)

| File | Tests | What It Covers |
|------|-------|----------------|
| `stacks.test.ts` | 18 | SharedStack (EventBridge, SSM), OrderPaymentStack (4 tables, 3 Lambdas, 4 queues, rules), InventoryStack (tables, Lambda, 5 rules), ShippingStack (table, GSI, rule), NotificationStack (Lambda, 3 rules) |

## How Tests Work

### Moto Mocking

All Python tests use [moto](https://github.com/getmoto/moto) to mock AWS services locally. The `aws_mock` fixture in `conftest.py`:

1. Starts moto's `mock_aws()` context
2. Creates all DynamoDB tables with the same schema as the CDK stacks
3. Creates the EventBridge event bus
4. Resets lazy-initialized boto3 clients in service modules
5. Tears everything down after each test

No real AWS credentials or infrastructure needed.

### CDK Assertions

CDK tests use `Template.fromStack()` to synthesize CloudFormation templates and then assert on resource properties. No deployment needed.
