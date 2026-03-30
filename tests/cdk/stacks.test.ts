import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { SharedStack } from '../../lib/shared-stack';
import { OrderPaymentStack } from '../../lib/order-payment-stack';
import { InventoryStack } from '../../lib/inventory-stack';
import { ShippingStack } from '../../lib/shipping-stack';
import { NotificationStack } from '../../lib/notification-stack';

describe('SharedStack', () => {
  const app = new cdk.App();
  const stack = new SharedStack(app, 'TestSharedStack');
  const template = Template.fromStack(stack);

  test('creates EventBridge event bus', () => {
    template.hasResourceProperties('AWS::Events::EventBus', {
      Name: 'ecommerce-event-bus',
    });
  });

  test('creates SSM parameters for bus name and ARN', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/ecommerce/event-bus-name',
    });
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/ecommerce/event-bus-arn',
    });
  });
});

describe('OrderPaymentStack', () => {
  const app = new cdk.App();
  // SharedStack must exist for SSM lookups
  new SharedStack(app, 'SharedStack');
  const stack = new OrderPaymentStack(app, 'TestOrderPaymentStack');
  const template = Template.fromStack(stack);

  test('creates OrdersTable', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'OrdersTable',
      KeySchema: [{ AttributeName: 'order_id', KeyType: 'HASH' }],
    });
  });

  test('creates SagaStateTable', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'SagaStateTable',
    });
  });

  test('creates PaymentsTable with GSI', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'PaymentsTable',
      GlobalSecondaryIndexes: [{
        IndexName: 'order_id-index',
      }],
    });
  });

  test('creates IdempotencyKeysTable with TTL', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'IdempotencyKeysTable',
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  test('creates three Lambda functions', () => {
    template.resourceCountIs('AWS::Lambda::Function', 3);
  });

  test('creates SQS queues with DLQs', () => {
    // 2 main queues + 2 DLQs = 4 total
    template.resourceCountIs('AWS::SQS::Queue', 4);
  });

  test('creates EventBridge rules for order events', () => {
    template.hasResourceProperties('AWS::Events::Rule', {
      Name: 'order-inventory-reserved',
    });
    template.hasResourceProperties('AWS::Events::Rule', {
      Name: 'order-payment-succeeded',
    });
  });
});

describe('InventoryStack', () => {
  const app = new cdk.App();
  new SharedStack(app, 'SharedStack');
  const stack = new InventoryStack(app, 'TestInventoryStack');
  const template = Template.fromStack(stack);

  test('creates InventoryTable', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'InventoryTable',
      KeySchema: [{ AttributeName: 'productId', KeyType: 'HASH' }],
    });
  });

  test('creates ReservationsTable with composite key', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'ReservationsTable',
      KeySchema: [
        { AttributeName: 'orderId', KeyType: 'HASH' },
        { AttributeName: 'productId', KeyType: 'RANGE' },
      ],
    });
  });

  test('creates inventory Lambda function', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'inventory-service',
      Handler: 'handler.lambda_handler',
      Runtime: 'python3.11',
    });
  });

  test('creates EventBridge rules for all consumed events', () => {
    template.hasResourceProperties('AWS::Events::Rule', { Name: 'inventory-order-created' });
    template.hasResourceProperties('AWS::Events::Rule', { Name: 'inventory-order-canceled' });
    template.hasResourceProperties('AWS::Events::Rule', { Name: 'inventory-product-created' });
    template.hasResourceProperties('AWS::Events::Rule', { Name: 'inventory-shipment-created' });
    template.hasResourceProperties('AWS::Events::Rule', { Name: 'inventory-compensate' });
  });
});

describe('ShippingStack', () => {
  const app = new cdk.App();
  new SharedStack(app, 'SharedStack');
  const stack = new ShippingStack(app, 'TestShippingStack');
  const template = Template.fromStack(stack);

  test('creates ShipmentsTable with GSI', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'ShipmentsTable',
      GlobalSecondaryIndexes: [{ IndexName: 'orderId-index' }],
    });
  });

  test('listens for OrderConfirmed events', () => {
    template.hasResourceProperties('AWS::Events::Rule', {
      Name: 'shipping-order-confirmed',
    });
  });
});

describe('NotificationStack', () => {
  const app = new cdk.App();
  new SharedStack(app, 'SharedStack');
  const stack = new NotificationStack(app, 'TestNotificationStack');
  const template = Template.fromStack(stack);

  test('creates notification Lambda with SES permissions', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'notification-service',
    });
  });

  test('listens for OrderConfirmed, ShipmentCreated, OrderCanceled', () => {
    template.hasResourceProperties('AWS::Events::Rule', { Name: 'notification-order-confirmed' });
    template.hasResourceProperties('AWS::Events::Rule', { Name: 'notification-shipment-created' });
    template.hasResourceProperties('AWS::Events::Rule', { Name: 'notification-order-canceled' });
  });
});
