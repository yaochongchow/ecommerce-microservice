#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { SharedStack } from '../lib/shared-stack';
import { InventoryStack } from '../lib/inventory-stack';
import { ShippingStack } from '../lib/shipping-stack';
import { NotificationStack } from '../lib/notification-stack';

const app = new cdk.App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION,
};

const shared = new SharedStack(app, 'SharedStack', { env });

const inventory = new InventoryStack(app, 'InventoryStack', { env });
const shipping = new ShippingStack(app, 'ShippingStack', { env });
const notification = new NotificationStack(app, 'NotificationStack', { env });

// Ensure SharedStack (event bus + SSM params) is deployed before service stacks
inventory.addDependency(shared);
shipping.addDependency(shared);
notification.addDependency(shared);
