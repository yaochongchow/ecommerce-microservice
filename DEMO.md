# M2 Demo — Order & Payment Saga in Action

This document walks through a live demonstration of the order and payment microservices. It covers the happy path, payment failure, inventory failure with compensation (refund), idempotency, and the circuit breaker.

---

## Prerequisites

Before running the demo, ensure the stack is deployed:

```bash
# Install dependencies
pip install -r requirements.txt

# Build and deploy
sam build && sam deploy --guided
```

After deployment, SAM outputs the API URL:

```
Outputs:
  OrderApiUrl: https://<api-id>.execute-api.us-east-1.amazonaws.com/Prod
```

Set it as a variable for convenience:

```bash
export API_URL="https://<api-id>.execute-api.us-east-1.amazonaws.com/Prod"
```

---

## Demo 1: Happy Path — Full Order Lifecycle

This shows the complete saga flow: create order → charge payment → reserve inventory → confirm order.

### Step 1: Create an Order

```bash
curl -s -X POST "$API_URL/orders" \
  -H "Content-Type: application/json" \
  -H "X-Correlation-Id: demo-001" \
  -d '{
    "user_id": "usr_demo001",
    "items": [
      {"product_id": "prod_001", "quantity": 2, "unit_price": 29.99},
      {"product_id": "prod_002", "quantity": 1, "unit_price": 49.99}
    ],
    "shipping_address": {
      "street": "123 Huntington Ave",
      "city": "Boston",
      "state": "MA",
      "zip": "02115"
    }
  }' | python3 -m json.tool
```

**Expected response (201):**

```json
{
  "message": "Order created",
  "order": {
    "order_id": "ord_a1b2c3d4e5f6",
    "user_id": "usr_demo001",
    "status": "PENDING",
    "total_amount": "109.97",
    "currency": "USD",
    "items": [...]
  }
}
```

**What happens behind the scenes:**
1. Order service creates the order in DynamoDB (`status: PENDING`).
2. Saga state is initialized in the saga-state table.
3. Saga transitions to `PAYMENT_PROCESSING`.
4. An `order.created` event is published to EventBridge.

Save the order ID:

```bash
export ORDER_ID="ord_a1b2c3d4e5f6"   # use the actual ID from the response
```

### Step 2: Watch the Saga Progress

The saga runs automatically through EventBridge events. Check the order status after a few seconds:

```bash
curl -s "$API_URL/orders/$ORDER_ID" | python3 -m json.tool
```

**Expected status progression (check every 2-3 seconds):**

| Time | Status | What Happened |
|------|--------|---------------|
| 0s | `PENDING` | Order created |
| ~1s | `PAYMENT_PROCESSING` | Waiting for Stripe charge |
| ~3s | `PAYMENT_COMPLETED` | Stripe charge succeeded |
| ~4s | `INVENTORY_RESERVING` | Waiting for M4 to reserve stock |
| ~6s | `CONFIRMED` | All steps succeeded — order finalized |

### Step 3: Verify in CloudWatch Logs

Search for the correlation ID to trace the full request across both services:

```
fields @timestamp, service, message, order_id, saga_state
| filter correlation_id = "demo-001"
| sort @timestamp asc
```

**Expected log sequence:**

```
[order-service]         API request received — POST /orders
[order-service-saga]    Saga started — awaiting payment
[payment-service]       Processing payment for order
[stripe-client]         Creating Stripe charge
[stripe-client]         Stripe charge created
[payment-service]       Payment completed successfully
[order-service-saga]    Payment completed — awaiting inventory reservation
[order-service-saga]    Order confirmed — saga complete
```

---

## Demo 2: Payment Failure — Card Declined

This shows what happens when the customer's card is declined. No compensation is needed because nothing succeeded before the failure.

### Step 1: Create an Order (with a card that will be declined)

In Stripe test mode, the token `tok_chargeDeclined` triggers a decline:

```bash
curl -s -X POST "$API_URL/orders" \
  -H "Content-Type: application/json" \
  -H "X-Correlation-Id: demo-002" \
  -d '{
    "user_id": "usr_demo002",
    "items": [
      {"product_id": "prod_003", "quantity": 1, "unit_price": 99.99}
    ]
  }' | python3 -m json.tool
```

### Step 2: Check the Order Status

```bash
curl -s "$API_URL/orders/$ORDER_ID" | python3 -m json.tool
```

**Expected result:**

```json
{
  "order": {
    "order_id": "ord_...",
    "status": "CANCELLED",
    "cancellation_reason": "Payment failed: Your card was declined"
  }
}
```

**Saga flow:**

```
PENDING → PAYMENT_PROCESSING → PAYMENT_FAILED → CANCELLED
```

No compensation is triggered because no money was charged.

---

## Demo 3: Inventory Failure — Compensation (Refund)

This is the most important demo — it shows the saga compensation pattern. Payment succeeds, but inventory is unavailable, so the system automatically refunds the customer.

### Step 1: Create an Order for an Out-of-Stock Item

```bash
curl -s -X POST "$API_URL/orders" \
  -H "Content-Type: application/json" \
  -H "X-Correlation-Id: demo-003" \
  -d '{
    "user_id": "usr_demo003",
    "items": [
      {"product_id": "prod_out_of_stock", "quantity": 100, "unit_price": 9.99}
    ]
  }' | python3 -m json.tool
```

### Step 2: Watch the Compensation Flow

Check status every 2-3 seconds:

```bash
curl -s "$API_URL/orders/$ORDER_ID" | python3 -m json.tool
```

**Expected status progression:**

| Time | Status | What Happened |
|------|--------|---------------|
| 0s | `PENDING` | Order created |
| ~3s | `PAYMENT_COMPLETED` | Stripe charged $999.00 |
| ~5s | `INVENTORY_RESERVING` | M4 inventory checking stock |
| ~7s | `COMPENSATING` | Inventory failed — refunding payment |
| ~10s | `CANCELLED` | Refund completed — order cancelled |

**Saga flow:**

```
PENDING → PAYMENT_PROCESSING → PAYMENT_COMPLETED → INVENTORY_RESERVING
    → INVENTORY_FAILED → COMPENSATING → CANCELLED
```

### Step 3: Verify the Refund

Check the saga state to see the full history:

```bash
# Query the saga-state table directly via AWS CLI
aws dynamodb get-item \
  --table-name dev-saga-state \
  --key '{"order_id": {"S": "'$ORDER_ID'"}}' \
  --query 'Item.history' | python3 -m json.tool
```

**Expected history (6 transitions):**

```json
[
  {"from_state": null, "to_state": "PENDING", "reason": "Order created"},
  {"from_state": "PENDING", "to_state": "PAYMENT_PROCESSING", "reason": "Initiating payment"},
  {"from_state": "PAYMENT_PROCESSING", "to_state": "PAYMENT_COMPLETED", "reason": "Payment completed: pay_..."},
  {"from_state": "PAYMENT_COMPLETED", "to_state": "INVENTORY_RESERVING", "reason": "Requesting inventory"},
  {"from_state": "INVENTORY_RESERVING", "to_state": "INVENTORY_FAILED", "reason": "Insufficient stock"},
  {"from_state": "INVENTORY_FAILED", "to_state": "COMPENSATING", "reason": "Starting compensation: refund"},
  {"from_state": "COMPENSATING", "to_state": "CANCELLED", "reason": "Payment refunded: re_..."}
]
```

You can also verify the refund in the Stripe Dashboard under **Payments → Refunded**.

---

## Demo 4: Idempotency — No Double Charges

This shows that retrying the same event does not charge the customer twice.

### Step 1: Simulate a Duplicate Event

Manually publish the same `order.created` event twice to EventBridge:

```bash
# Publish the event
aws events put-events --entries '[{
  "Source": "ecommerce.m2",
  "DetailType": "order.created",
  "Detail": "{\"metadata\":{\"correlation_id\":\"demo-004\"},\"data\":{\"order_id\":\"ord_idem_test\",\"user_id\":\"usr_demo004\",\"items\":[{\"product_id\":\"p1\",\"quantity\":1,\"unit_price\":50.00}],\"total_amount\":50.00,\"currency\":\"USD\",\"idempotency_key\":\"idem_demo004\"}}",
  "EventBusName": "ecommerce-event-bus"
}]'

# Wait 2 seconds, then send the SAME event again (simulating retry)
sleep 2

aws events put-events --entries '[{
  "Source": "ecommerce.m2",
  "DetailType": "order.created",
  "Detail": "{\"metadata\":{\"correlation_id\":\"demo-004\"},\"data\":{\"order_id\":\"ord_idem_test\",\"user_id\":\"usr_demo004\",\"items\":[{\"product_id\":\"p1\",\"quantity\":1,\"unit_price\":50.00}],\"total_amount\":50.00,\"currency\":\"USD\",\"idempotency_key\":\"idem_demo004\"}}",
  "EventBusName": "ecommerce-event-bus"
}]'
```

### Step 2: Verify Only One Charge

Check the payments table:

```bash
aws dynamodb query \
  --table-name dev-payments \
  --index-name order_id-index \
  --key-condition-expression "order_id = :oid" \
  --expression-attribute-values '{":oid": {"S": "ord_idem_test"}}' \
  | python3 -m json.tool
```

**Expected:** Only **one** payment record exists, not two.

Check the CloudWatch logs — you should see:

```
[payment-service]       New idempotency key — processing payment         ← First event
[payment-idempotency]   Payment result cached for idempotency key
...
[payment-idempotency]   Idempotency key found — returning cached result  ← Retry (no charge)
```

---

## Demo 5: User-Initiated Cancellation

This shows a user cancelling an order that's still in progress.

### Step 1: Create an Order

```bash
curl -s -X POST "$API_URL/orders" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "usr_demo005", "items": [{"product_id": "p1", "quantity": 1, "unit_price": 25.00}]}' \
  | python3 -m json.tool
```

### Step 2: Cancel Before Payment Completes

```bash
curl -s -X PUT "$API_URL/orders/$ORDER_ID/cancel" | python3 -m json.tool
```

**If payment hasn't been charged yet:**

```json
{
  "message": "Order cancelled",
  "order_id": "ord_..."
}
```

**If payment was already charged:**

```json
{
  "message": "Cancellation initiated — refund in progress",
  "order_id": "ord_..."
}
```

In the second case, the compensation flow triggers automatically to refund the payment.

---

## Demo 6: Running the Tests

Show that all unit tests pass using mocked AWS (no real infrastructure needed):

```bash
pytest tests/ -v
```

**Expected output:**

```
tests/test_order_handler.py::TestCreateOrder::test_create_order_success         PASSED
tests/test_order_handler.py::TestCreateOrder::test_create_order_missing_user_id PASSED
tests/test_order_handler.py::TestCreateOrder::test_create_order_missing_items   PASSED
tests/test_order_handler.py::TestCreateOrder::test_create_order_invalid_item    PASSED
tests/test_order_handler.py::TestGetOrder::test_get_order_success              PASSED
tests/test_order_handler.py::TestGetOrder::test_get_order_not_found            PASSED
tests/test_order_handler.py::TestCancelOrder::test_cancel_pending_order        PASSED
tests/test_order_handler.py::TestCancelOrder::test_cancel_nonexistent_order    PASSED
tests/test_order_handler.py::TestRouting::test_unknown_route_returns_404       PASSED
tests/test_payment_handler.py::TestOrderCreatedEvent::test_successful_payment  PASSED
tests/test_payment_handler.py::TestOrderCreatedEvent::test_idempotent_retry    PASSED
tests/test_payment_handler.py::TestOrderCreatedEvent::test_payment_card_declined PASSED
tests/test_payment_handler.py::TestCompensatePaymentEvent::test_successful_refund PASSED
tests/test_saga.py::TestSagaHappyPath::test_start_saga                        PASSED
tests/test_saga.py::TestSagaHappyPath::test_payment_completed_advances        PASSED
tests/test_saga.py::TestSagaHappyPath::test_inventory_reserved_confirms       PASSED
tests/test_saga.py::TestSagaPaymentFailure::test_payment_failed_cancels       PASSED
tests/test_saga.py::TestSagaCompensation::test_inventory_failed_compensates   PASSED
tests/test_saga.py::TestSagaStateHistory::test_saga_records_history           PASSED

==================== 19 passed ====================
```

---

## Demo Script Summary

| # | Demo | What It Proves | Key Pattern |
|---|------|---------------|-------------|
| 1 | Happy path | Full saga completes across services | Saga orchestration |
| 2 | Card declined | Failure before any success — clean cancel | Saga failure handling |
| 3 | Inventory failure | Payment refunded automatically after downstream failure | **Saga compensation** |
| 4 | Duplicate event | Same event processed twice — only one charge | **Idempotency** |
| 5 | User cancellation | Cancel triggers refund if payment already charged | API + compensation |
| 6 | Unit tests | All logic works with mocked AWS — no infra needed | Testability |

---

## Talking Points for Presentation

When demoing, highlight these design decisions:

1. **"Why not just call the payment service directly?"**
   → Services communicate via events (EventBridge) so they're decoupled. The order service doesn't need to know the payment service's URL, and if payment is slow, it doesn't block the API response.

2. **"What happens if the same event is delivered twice?"**
   → The idempotency key table catches duplicates. We have double protection — our DynamoDB check AND Stripe's built-in idempotency.

3. **"What if Stripe goes down?"**
   → The circuit breaker trips after 5 consecutive failures. Instead of waiting for timeouts (expensive on Lambda), it fails fast. The failed event goes to a dead-letter queue for retry when Stripe recovers.

4. **"How do you debug a failed order?"**
   → Every request has a correlation ID that flows through all services. Search CloudWatch Logs with that ID to see the full trace. The saga state table also records every transition with timestamps and reasons.

5. **"What if the refund itself fails?"**
   → The saga enters a `COMPENSATING` state and the event goes to the DLQ. A CloudWatch alarm (set up by M1) fires, and the operations team can manually retry or investigate.
