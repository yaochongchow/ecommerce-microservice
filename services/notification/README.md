# Notification Service

Sends transactional emails to customers in response to order lifecycle events. Stateless — it holds no database of its own. All information needed to compose and send an email must arrive in the event payload.

Emails are sent via AWS SES in production and logged as mock output during development, controlled by the `EMAIL_MODE` environment variable.

Events are delivered via an SQS queue (`notification-service-queue`) that sits between EventBridge and the Lambda. Failed messages are routed to a dead-letter queue (`notification-service-dlq`) after 3 attempts.

---

## Events consumed

### From `payment-service`

#### `PaymentSucceeded`

Sends an order confirmation email to the customer after payment is processed.

```json
{
  "source": "payment-service",
  "detail-type": "PaymentSucceeded",
  "detail": {
    "orderId": "ord_123",
    "email": "customer@example.com",
    "items": [
      { "productId": "prod_001", "productName": "Widget", "quantity": 2 }
    ]
  }
}
```

> **Required fields:** `orderId`, `email`.
> `items[]` is optional but recommended — if omitted, the confirmation email will not list individual items. `productName` is used if present; falls back to `productId`.

---

### From `order-service`

#### `OrderCanceled`

Sends a cancellation confirmation email to the customer. Only sent when the order was cancelled after the customer had already placed it (e.g. customer-initiated cancellation or post-payment failure). Orders that fail at inventory reservation never reach payment, so no cancellation email is needed in that case.

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

> **Required fields:** `orderId`, `email`.
> `reason` is optional but strongly recommended — it is shown directly in the cancellation email so the customer understands why. Falls back to a generic message if omitted.

---

### From `shipping-service`

#### `ShipmentCreated`

Sends a shipment notification email with carrier and tracking number.

```json
{
  "source": "shipping-service",
  "detail-type": "ShipmentCreated",
  "detail": {
    "orderId": "ord_123",
    "email": "customer@example.com",
    "carrier": "UPS_MOCK",
    "trackingNumber": "MOCK-20260328-0042"
  }
}
```

> **Required fields:** `orderId`, `email`, `carrier`, `trackingNumber`.

---

## Events published

None. The notification service is a terminal consumer — it reacts to events but does not emit any of its own.

---

## Email mode

Controlled by the `EMAIL_MODE` environment variable:

| Value  | Behavior                                   |
| ------ | ------------------------------------------ |
| `mock` | Logs email content to CloudWatch (default) |
| `ses`  | Sends email via AWS SES                    |

To switch to real email delivery:

1. Set `EMAIL_MODE=ses` in the Lambda environment (CDK stack or console).
2. Set `SES_FROM_EMAIL` to a verified sender address in SES.
3. Verify the recipient domain or move SES out of sandbox mode for production use.

---

## Emails sent

| Trigger            | Subject                         | Content                            |
| ------------------ | ------------------------------- | ---------------------------------- |
| `PaymentSucceeded` | "Order Confirmation"            | Order ID, item list                |
| `ShipmentCreated`  | "Your Order Has Shipped"        | Order ID, carrier, tracking number |
| `OrderCanceled`    | "Your Order Has Been Cancelled" | Order ID, refund notice            |

---

## Contract requirements from other services

### payment-service

- Must include `email` and `orderId` in `PaymentSucceeded`.
- Should include `items[]` with `productId`, `quantity`, and optionally `productName` for a complete order confirmation.

### shipping-service

- Must include `email`, `carrier`, and `trackingNumber` in `ShipmentCreated`.

### order-service

- Must include `email` and `orderId` in `OrderCanceled` so the customer can be notified.
- Should only emit `OrderCanceled` for orders the customer is aware of (i.e. after the order was confirmed, not for silent internal failures like inventory reservation failures before payment).
- Should include a `reason` field describing why the order was cancelled (e.g. `"cancelled by customer"`, `"payment failed"`). Omitting it results in a generic message to the customer.

---

## Known limitations

- **No retry on send failure.** If SES rejects the email (e.g. unverified address in sandbox mode), the error is logged and re-raised. EventBridge will retry the Lambda invocation, which may result in duplicate emails on a later success.
- **No deduplication.** If the same event is delivered more than once, the customer will receive duplicate emails. Downstream deduplication (e.g. tracking sent message IDs) is not implemented.
- **Plain text only.** Emails are sent as plain text. HTML templates are not supported in the current implementation.
