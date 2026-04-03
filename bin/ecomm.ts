#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { SharedStack } from '../lib/shared-stack';
import { PlatformStack } from '../lib/platform-stack';
import { OrderPaymentStack } from '../lib/order-payment-stack';
import { ProductCartStack } from '../lib/product-cart-stack';
import { InventoryStack } from '../lib/inventory-stack';
import { ShippingStack } from '../lib/shipping-stack';
import { NotificationStack } from '../lib/notification-stack';

const app = new cdk.App();

const env: cdk.Environment = {
  account: app.node.tryGetContext('account') ?? process.env.CDK_DEFAULT_ACCOUNT,
  region:  app.node.tryGetContext('region')  ?? process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
};

// 1. Shared infrastructure — EventBridge bus, SSM parameters
const shared = new SharedStack(app, 'SharedStack', { env });

// 2. Order + Payment — saga, idempotent payments, DynamoDB tables
const orderPayment = new OrderPaymentStack(app, 'OrderPaymentStack', { env });

// 3. Product + Cart — ECS Fargate, ALB, DynamoDB, Redis sidecar
const productCart = new ProductCartStack(app, 'ProductCartStack', { env });

// 4. Platform — Cognito, API Gateway, CloudFront, BFF, User service
const platform = new PlatformStack(app, 'PlatformStack', { env });

// 5. Inventory — stock reservation/release, optimistic locking
const inventory = new InventoryStack(app, 'InventoryStack', { env });

// 6. Shipping — carrier integration, tracking
const shipping = new ShippingStack(app, 'ShippingStack', { env });

// 7. Notification — SES email, templates
const notification = new NotificationStack(app, 'NotificationStack', { env });

// Stack dependencies — SharedStack must deploy first
[orderPayment, productCart, platform, inventory, shipping, notification]
  .forEach(s => s.addDependency(shared));
platform.addDependency(orderPayment);
platform.addDependency(productCart);
platform.addDependency(inventory);
