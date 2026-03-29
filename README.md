# ShopCloud — E-Commerce Platform

A serverless e-commerce platform built on AWS CDK (TypeScript).

---

## Architecture

| Layer | Services |
|---|---|
| Frontend | S3 + CloudFront |
| Auth | Cognito User Pool |
| API | HTTP API Gateway + BFF Lambda |
| User service | Lambda + DynamoDB (users, carts, orders, sessions) |
| Eventing | EventBridge + SQS Dead Letter Queue |
| Observability | X-Ray tracing + CloudWatch alarms |
| Deployment | CDK + S3 BucketDeployment (auto-uploads frontend) |

---

## Prerequisites

- [Node.js 20+](https://nodejs.org)
- [AWS CLI](https://aws.amazon.com/cli/) configured with your `mowat_admin` profile
- [AWS CDK CLI v2](https://docs.aws.amazon.com/cdk/latest/guide/cli.html)

Verify everything is set up:

```bash
node --version       # v20+
aws --version        # aws-cli/2.x
cdk --version        # 2.x
aws sts get-caller-identity --profile mowat_admin
```

---

## 1. Install dependencies

```bash
npm install
```

---

## 2. Bootstrap CDK (first time only)

```bash
cdk bootstrap --profile mowat_admin
```

You only need to do this once per account/region.

---

## 3. Synthesize

```bash
npx cdk synth --profile mowat_admin
```

Compiles TypeScript and generates the CloudFormation template. Catches errors before deploy.

---

## 4. Deploy

```bash
cdk deploy --profile mowat_admin
```

- Prompts for confirmation before deploying — type `y` to proceed
- Automatically uploads `frontend/` to S3 and invalidates CloudFront
- Takes approximately **3–5 minutes**

When complete, CDK prints:

```
Outputs:
EcommPlatformStack.ApiUrl           = https://abc123.execute-api.us-east-1.amazonaws.com
EcommPlatformStack.CloudFrontDomain = https://d1234abcd.cloudfront.net
EcommPlatformStack.UserPoolId       = us-east-1_XXXXXXX
EcommPlatformStack.UserPoolClientId = xxxxxxxxxxxxxxxxxxxxxxxxxx
EcommPlatformStack.FrontendBucketName = ecommplatformstack-frontendbucket-xxxx
```

---

## 5. Configure the frontend

Open `frontend/index.html` and update the `CONFIG` block with your CDK output values:

```js
const CONFIG = {
  apiUrl:     "https://abc123.execute-api.us-east-1.amazonaws.com",
  userPoolId: "us-east-1_XXXXXXX",
  clientId:   "xxxxxxxxxxxxxxxxxxxxxxxxxx",
  region:     "us-east-1",
};
```

Then redeploy to push the updated config:

```bash
cdk deploy --profile mowat_admin
```

---

## 6. Create sample users

```bash
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name EcommPlatformStack \
  --profile mowat_admin \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
  --output text)

# Alice
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username alice@shopcloud.dev \
  --user-attributes Name=email,Value=alice@shopcloud.dev Name=given_name,Value=Alice Name=family_name,Value=Johnson Name=email_verified,Value=true \
  --temporary-password "Temp1234!" --message-action SUPPRESS --profile mowat_admin

aws cognito-idp admin-set-user-password \
  --user-pool-id $USER_POOL_ID --username alice@shopcloud.dev \
  --password "Alice1234!" --permanent --profile mowat_admin

# Bob
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username bob@shopcloud.dev \
  --user-attributes Name=email,Value=bob@shopcloud.dev Name=given_name,Value=Bob Name=family_name,Value=Smith Name=email_verified,Value=true \
  --temporary-password "Temp1234!" --message-action SUPPRESS --profile mowat_admin

aws cognito-idp admin-set-user-password \
  --user-pool-id $USER_POOL_ID --username bob@shopcloud.dev \
  --password "Bob12345!" --permanent --profile mowat_admin

# Carol
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username carol@shopcloud.dev \
  --user-attributes Name=email,Value=carol@shopcloud.dev Name=given_name,Value=Carol Name=family_name,Value=Williams Name=email_verified,Value=true \
  --temporary-password "Temp1234!" --message-action SUPPRESS --profile mowat_admin

aws cognito-idp admin-set-user-password \
  --user-pool-id $USER_POOL_ID --username carol@shopcloud.dev \
  --password "Carol123!" --permanent --profile mowat_admin
```

| Name | Email | Password |
|---|---|---|
| Alice Johnson | alice@shopcloud.dev | Alice1234! |
| Bob Smith | bob@shopcloud.dev | Bob12345! |
| Carol Williams | carol@shopcloud.dev | Carol123! |

---

## 7. View the site

Open your CloudFront domain from the CDK outputs:

```
https://d1234abcd.cloudfront.net
```

> CloudFront may take 2–3 minutes to propagate on first deploy.

---

## API routes

| Method | Path | Auth | Handler |
|---|---|---|---|
| GET | /health | — | BFF |
| GET | /api/products | — | BFF |
| GET | /api/products/{id} | — | BFF |
| GET | /api/search?q= | — | BFF |
| POST | /api/orders | JWT | BFF |
| GET | /api/orders/{id} | JWT | BFF |
| GET | /api/me | JWT | User service |
| PUT | /api/me | JWT | User service |
| GET | /api/me/cart | JWT | User service |
| POST | /api/me/cart | JWT | User service |
| DELETE | /api/me/cart/{itemId} | JWT | User service |
| GET | /api/me/orders | JWT | User service |

---

## Updating the frontend

Any time you change `frontend/index.html`, just redeploy — CDK handles the S3 upload and CloudFront cache invalidation automatically:

```bash
cdk deploy --profile mowat_admin
```

---

## Estimated costs

| Service | Cost |
|---|---|
| CloudFront | ~$0 under free tier |
| API Gateway | ~$0 under 1M requests/month |
| Lambda | ~$0 under 1M invocations/month |
| DynamoDB | ~$0 on-demand with low traffic |
| S3 | ~$0.023/GB stored |
| SNS / SQS / EventBridge | ~$0 at this scale |

> WAF has been intentionally removed to avoid the $7/month flat charge. Add it back before any public launch.

---

## Tear down

```bash
cdk destroy --profile mowat_admin
```

> **Warning:** This permanently deletes all data including DynamoDB tables and the S3 bucket.

---

## Troubleshooting

**Blank page or 403**
CloudFront may still be propagating. Wait 2–3 minutes and refresh.

**API calls failing**
Check the `CONFIG` block in `index.html` — make sure `apiUrl` has no trailing slash.

**`cdk deploy` fails with permissions error**
Ensure `mowat_admin` has AdministratorAccess or equivalent IAM permissions.

**Changes not showing after redeploy**
CDK automatically invalidates CloudFront on deploy via `BucketDeployment`. If you uploaded manually, run:
```bash
aws cloudfront create-invalidation \
  --distribution-id YOUR_DISTRIBUTION_ID \
  --paths "/*" --profile mowat_admin
```