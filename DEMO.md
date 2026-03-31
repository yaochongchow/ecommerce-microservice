# Demo Guide — ShopCloud E-Commerce Platform

Step-by-step instructions to deploy, test, and demonstrate the full platform.

## Prerequisites

- [Node.js 20+](https://nodejs.org)
- [AWS CLI v2](https://aws.amazon.com/cli/) configured with credentials
- [AWS CDK CLI v2](https://docs.aws.amazon.com/cdk/latest/guide/cli.html)
- [Docker](https://www.docker.com/) (for building ECS container images)
- [Go 1.23+](https://go.dev/) (optional, for local product/cart development)
- [Python 3.11+](https://www.python.org/) (optional, for local Lambda development)

Verify setup:

```bash
node --version            # v20+
aws --version             # aws-cli/2.x
cdk --version             # 2.x
docker --version          # Docker 24+
aws sts get-caller-identity   # confirm AWS credentials work
```

---

## 1. Install Dependencies

```bash
cd ecommerce-microservice
npm install
```

---

## 2. Bootstrap CDK (First Time Only)

```bash
npx cdk bootstrap
```

---

## 3. Build

```bash
npm run build
```

This compiles all TypeScript CDK stacks. Fix any errors before deploying.

---

## 4. Synthesize (Optional — Validates Templates)

```bash
npx cdk synth
```

Generates CloudFormation templates without deploying. Good for catching issues early.

---

## 5. Deploy

### Deploy All Stacks

```bash
npx cdk deploy --all --require-approval never
```

This deploys 7 stacks in dependency order:

1. **SharedStack** — EventBridge bus, SSM params
2. **OrderPaymentStack** — Order + Payment Lambdas, DynamoDB tables, SQS queues
3. **ProductCartStack** — VPC, ECS cluster, ALB, Fargate services
4. **PlatformStack** — Cognito, API Gateway, CloudFront, BFF, User service
5. **InventoryStack** — Inventory Lambda, DynamoDB tables, SQS queues
6. **ShippingStack** — Shipping Lambda, DynamoDB tables, SQS queues
7. **NotificationStack** — Notification Lambda, SQS queues

### Deploy Without ECS (Faster, No Docker Required)

If you want to skip the Product/Cart ECS services (which take longer and require Docker):

```bash
npx cdk deploy SharedStack OrderPaymentStack PlatformStack InventoryStack ShippingStack NotificationStack
```

The BFF will use its built-in demo product catalog as a fallback.

---

## 6. Note the Outputs

After deployment, CDK prints outputs like:

```
PlatformStack.ApiUrl           = https://abc123.execute-api.us-east-1.amazonaws.com
PlatformStack.CloudFrontDomain = https://d1234abcd.cloudfront.net
PlatformStack.UserPoolId       = us-east-1_XXXXXXX
PlatformStack.UserPoolClientId = xxxxxxxxxxxxxxxxxxxxxxxxxx
```

Save these — you'll need them for testing.

---

## 7. Configure the Frontend

Open `frontend/index.html` and update the `CONFIG` block with your CDK output values:

```js
const CONFIG = {
  apiUrl:     "https://abc123.execute-api.us-east-1.amazonaws.com",
  userPoolId: "us-east-1_XXXXXXX",
  clientId:   "xxxxxxxxxxxxxxxxxxxxxxxxxx",
  region:     "us-east-1",
};
```

Then redeploy the platform stack to push the updated frontend:

```bash
npx cdk deploy PlatformStack
```

---

## 8. Create Test Users

```bash
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name PlatformStack \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
  --output text)

# Create Alice
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username alice@shopcloud.dev \
  --user-attributes Name=email,Value=alice@shopcloud.dev Name=given_name,Value=Alice Name=family_name,Value=Johnson Name=email_verified,Value=true \
  --temporary-password "Temp1234!" --message-action SUPPRESS

aws cognito-idp admin-set-user-password \
  --user-pool-id $USER_POOL_ID --username alice@shopcloud.dev \
  --password "Alice1234!" --permanent
```

| Name | Email | Password |
|------|-------|----------|
| Alice Johnson | alice@shopcloud.dev | Alice1234! |

---

## 9. Demo the Full Flow

### 9a. Browse Products (No Auth Required)

```bash
API_URL=$(aws cloudformation describe-stacks \
  --stack-name PlatformStack \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text)

# Health check
curl -s "$API_URL/health" | jq .

# List products
curl -s "$API_URL/api/products" | jq .

# Search
curl -s "$API_URL/api/search?q=keyboard" | jq .
```

### 9b. Place an Order (Triggers Full Saga)

```bash
# Get a JWT token (replace with your user pool details)
TOKEN=$(aws cognito-idp initiate-auth \
  --client-id YOUR_CLIENT_ID \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=alice@shopcloud.dev,PASSWORD=Alice1234! \
  --query "AuthenticationResult.IdToken" --output text)

# Create an order — triggers the saga:
# OrderCreated -> InventoryReserved -> OrderReadyForPayment ->
# PaymentSucceeded -> OrderConfirmed -> ShipmentCreated
curl -s -X POST "$API_URL/api/orders" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"productId": "p1", "quantity": 1, "price": 89.99},
      {"productId": "p3", "quantity": 2, "price": 74.99}
    ],
    "total": 239.97,
    "shippingAddress": {"street": "123 Main St", "city": "Boston", "state": "MA", "zip": "02101"}
  }' | jq .
```

### 9c. Watch the Event Flow in CloudWatch

Open the AWS Console and navigate to **CloudWatch > Log Groups**. Watch these in order:

1. `/aws/lambda/order-api-service` — Order creation
2. `/aws/lambda/inventory-service` — Stock reservation
3. `/aws/lambda/order-event-service` — Saga processing
4. `/aws/lambda/payment-event-service` — Payment processing
5. `/aws/lambda/shipping-service` — Shipment creation
6. `/aws/lambda/notification-service` — Email notifications

### 9d. Check Order Status

```bash
ORDER_ID="<from the create order response>"

curl -s "$API_URL/api/orders/$ORDER_ID" \
  -H "Authorization: Bearer $TOKEN" | jq .

# View order history
curl -s "$API_URL/api/me/orders" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### 9e. Use the Frontend

Open the CloudFront URL in your browser:

```
https://d1234abcd.cloudfront.net
```

1. Browse the product catalog
2. Add items to cart
3. Sign in with Alice's credentials
4. Place an order
5. Check order history

---

## 10. EventBridge Event Inspection

View events flowing through the bus:

```bash
# Check DLQ for failed events
aws sqs get-queue-attributes \
  --queue-url $(aws cloudformation describe-stacks --stack-name InventoryStack \
    --query "Stacks[0].Outputs[?OutputKey=='InventoryDLQUrl'].OutputValue" --output text) \
  --attribute-names ApproximateNumberOfMessages | jq .
```

---

## 11. Tear Down

```bash
npx cdk destroy --all
```

**Warning:** This permanently deletes all data (DynamoDB tables, S3 buckets, etc.).

---

## Troubleshooting

**CDK deploy fails with "resource already exists"**
A previous deployment may have left orphaned resources. Delete them manually in the AWS Console, then retry.

**Order stays in PENDING status**
Check the order-event-service and inventory-service CloudWatch logs. The inventory service may be waiting for the product to exist in the InventoryTable.

**"Module not found" errors in Lambda**
The Lambda layer may not have been built correctly. Ensure `layers/common/python/` contains both `common/` and `shared/` directories.

**ECS tasks keep restarting**
Check the task logs in CloudWatch. The product service needs the DynamoDB `products` table to exist and be accessible. The cart service needs Redis (running as a sidecar).

**Frontend shows blank page**
CloudFront may need 2-3 minutes to propagate after first deploy. Check that `CONFIG` in `index.html` has correct values.

**CORS errors in browser**
Ensure the API Gateway CORS settings include your CloudFront domain. The current config allows `*` origins.
