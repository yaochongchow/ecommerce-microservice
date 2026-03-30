# Ecommerce Event-Driven Microservices

A serverless ecommerce backend built with AWS CDK (TypeScript) and Python Lambdas. Services communicate exclusively through a shared EventBridge event bus — no direct service-to-service calls.

## Services

**Owned by this repo:**

| Service                                        | Description                                                                                           |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| [inventory-service](services/inventory/)       | Reserves, releases, and fulfills stock. Tracks per-order reservations and publishes low-stock alerts. |
| [shipping-service](services/shipping/)         | Creates shipment records and issues tracking numbers on payment confirmation.                         |
| [notification-service](services/notification/) | Sends transactional emails (order confirmation, shipment, cancellation) via AWS SES.                  |

**External dependencies (teammate services):**

| Service           | Events published                                 | Consumed by                             |
| ----------------- | ------------------------------------------------ | --------------------------------------- |
| `order-service`   | `OrderCreated`, `OrderCanceled`, `OrderReturned` | inventory-service, notification-service |
| `payment-service` | `PaymentSucceeded`                               | shipping-service, notification-service  |
| `product-service` | `ProductCreated`, `ProductRestocked`             | inventory-service                       |

## Architecture

```mermaid
sequenceDiagram
    autonumber
    actor Customer

    Customer->>+order-service: Place order
    order-service->>EventBus: OrderCreated
    EventBus->>+inventory-service: OrderCreated
    inventory-service-->>EventBus: InventoryReserved
    inventory-service-->>-EventBus: InventoryReservationFailed

    Note over order-service: On InventoryReservationFailed,<br/>order-service cancels the order

    order-service->>+payment-service: Process payment
    payment-service->>EventBus: PaymentSucceeded
    deactivate payment-service
    EventBus->>+shipping-service: PaymentSucceeded
    EventBus->>+notification-service: PaymentSucceeded
    notification-service-->>-Customer: Order Confirmation email

    shipping-service-->>EventBus: ShipmentCreated
    deactivate shipping-service
    EventBus->>+inventory-service: ShipmentCreated
    inventory-service-->>-EventBus: InventoryFulfilled
    EventBus->>+notification-service: ShipmentCreated
    notification-service-->>-Customer: Shipment + Tracking email

    Note over product-service: Product catalog events:
    product-service->>EventBus: ProductCreated / ProductRestocked
    EventBus->>inventory-service: ProductCreated / ProductRestocked

    Note over order-service: If customer cancels after paying:
    order-service->>EventBus: OrderCanceled
    EventBus->>+inventory-service: OrderCanceled
    inventory-service-->>-EventBus: InventoryReleased
    EventBus->>+notification-service: OrderCanceled
    notification-service-->>-Customer: Cancellation email
```

## Stacks

| Stack               | Resources                                                                     |
| ------------------- | ----------------------------------------------------------------------------- |
| `SharedStack`       | EventBridge bus, SSM parameters                                               |
| `InventoryStack`    | InventoryTable, ReservationsTable, Lambda, SQS queue + DLQ, EventBridge rules |
| `ShippingStack`     | ShipmentsTable (+ orderId GSI), Lambda, SQS queue + DLQ, EventBridge rule     |
| `NotificationStack` | Lambda, SES policy, SQS queue + DLQ, EventBridge rules                        |

## Deploy

```bash
npm run build
npx cdk deploy --all
```

Requires AWS CLI configured with valid credentials (`aws sts get-caller-identity` to verify).

## Demo

```bash
source .venv/bin/activate
python3 scripts/demo.py
```

Fires the full happy-path event sequence against the deployed stack and prints CloudWatch log output from each service in real time.

## Useful commands

| Command                | Description                             |
| ---------------------- | --------------------------------------- |
| `npm run build`        | Compile TypeScript                      |
| `npx cdk synth`        | Synthesize CloudFormation templates     |
| `npx cdk deploy --all` | Deploy all stacks                       |
| `npx cdk diff`         | Compare deployed stack with local state |
