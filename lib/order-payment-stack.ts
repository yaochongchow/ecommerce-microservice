import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as path from 'path';

export class OrderPaymentStack extends cdk.Stack {
  public readonly orderApiFn: lambda.Function;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const eventBusArn = ssm.StringParameter.valueForStringParameter(this, '/ecommerce/event-bus-arn');
    const eventBusName = ssm.StringParameter.valueForStringParameter(this, '/ecommerce/event-bus-name');
    const eventBus = events.EventBus.fromEventBusArn(this, 'SharedBus', eventBusArn);

    const commonLayer = new lambda.LayerVersion(this, 'CommonLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../layers/common')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'Shared utilities (common + shared modules)',
    });

    // ── DynamoDB tables ──────────────────────────────────────────────────────
    const ordersTable = new dynamodb.Table(this, 'OrdersTable', {
      tableName: 'OrdersTable',
      partitionKey: { name: 'order_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const sagaStateTable = new dynamodb.Table(this, 'SagaStateTable', {
      tableName: 'SagaStateTable',
      partitionKey: { name: 'order_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const paymentsTable = new dynamodb.Table(this, 'PaymentsTable', {
      tableName: 'PaymentsTable',
      partitionKey: { name: 'payment_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    paymentsTable.addGlobalSecondaryIndex({
      indexName: 'order_id-index',
      partitionKey: { name: 'order_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    const idempotencyTable = new dynamodb.Table(this, 'IdempotencyKeysTable', {
      tableName: 'IdempotencyKeysTable',
      partitionKey: { name: 'idempotency_key', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: 'ttl',
    });

    // ── Order API Lambda (HTTP from API Gateway via BFF) ─────────────────────
    this.orderApiFn = new lambda.Function(this, 'OrderApiFunction', {
      functionName: 'order-api-service',
      runtime: lambda.Runtime.PYTHON_3_11,
      code: lambda.Code.fromAsset(path.join(__dirname, '../services/order')),
      handler: 'handler.api_handler',
      layers: [commonLayer],
      logGroup: new logs.LogGroup(this, 'OrderApiFnLogGroup', {
        logGroupName: '/aws/lambda/order-api-service',
        retention: logs.RetentionDays.TWO_WEEKS,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        ORDERS_TABLE: ordersTable.tableName,
        SAGA_STATE_TABLE: sagaStateTable.tableName,
        EVENT_BUS_NAME: eventBusName,
        LOG_LEVEL: 'INFO',
      },
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
    });
    ordersTable.grantReadWriteData(this.orderApiFn);
    sagaStateTable.grantReadWriteData(this.orderApiFn);
    eventBus.grantPutEventsTo(this.orderApiFn);

    // ── Order Event Lambda (EventBridge → SQS → Lambda) ──────────────────────
    const orderEventFn = new lambda.Function(this, 'OrderEventFunction', {
      functionName: 'order-event-service',
      runtime: lambda.Runtime.PYTHON_3_11,
      code: lambda.Code.fromAsset(path.join(__dirname, '../services/order')),
      handler: 'handler.event_handler',
      layers: [commonLayer],
      logGroup: new logs.LogGroup(this, 'OrderEventFnLogGroup', {
        logGroupName: '/aws/lambda/order-event-service',
        retention: logs.RetentionDays.TWO_WEEKS,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        ORDERS_TABLE: ordersTable.tableName,
        SAGA_STATE_TABLE: sagaStateTable.tableName,
        EVENT_BUS_NAME: eventBusName,
        LOG_LEVEL: 'INFO',
      },
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
    });
    ordersTable.grantReadWriteData(orderEventFn);
    sagaStateTable.grantReadWriteData(orderEventFn);
    eventBus.grantPutEventsTo(orderEventFn);

    const orderDlq = new sqs.Queue(this, 'OrderDLQ', {
      queueName: 'order-service-dlq',
      retentionPeriod: cdk.Duration.days(14),
    });
    const orderQueue = new sqs.Queue(this, 'OrderQueue', {
      queueName: 'order-service-queue',
      visibilityTimeout: cdk.Duration.seconds(60),
      deadLetterQueue: { queue: orderDlq, maxReceiveCount: 3 },
    });
    orderEventFn.addEventSource(new lambdaEventSources.SqsEventSource(orderQueue, { batchSize: 1 }));

    // EventBridge rules — route inventory + payment events to order queue
    new events.Rule(this, 'InventoryReservedRule', {
      eventBus, ruleName: 'order-inventory-reserved',
      eventPattern: { source: ['inventory-service'], detailType: ['InventoryReserved'] },
      targets: [new targets.SqsQueue(orderQueue)],
    });
    new events.Rule(this, 'InventoryFailedRule', {
      eventBus, ruleName: 'order-inventory-failed',
      eventPattern: { source: ['inventory-service'], detailType: ['InventoryReservationFailed'] },
      targets: [new targets.SqsQueue(orderQueue)],
    });
    new events.Rule(this, 'PaymentSucceededRule', {
      eventBus, ruleName: 'order-payment-succeeded',
      eventPattern: { source: ['payment-service'], detailType: ['PaymentSucceeded'] },
      targets: [new targets.SqsQueue(orderQueue)],
    });
    new events.Rule(this, 'PaymentFailedRule', {
      eventBus, ruleName: 'order-payment-failed',
      eventPattern: { source: ['payment-service'], detailType: ['PaymentFailed'] },
      targets: [new targets.SqsQueue(orderQueue)],
    });
    new events.Rule(this, 'InventoryReleasedRule', {
      eventBus, ruleName: 'order-inventory-released',
      eventPattern: { source: ['inventory-service'], detailType: ['InventoryReleased'] },
      targets: [new targets.SqsQueue(orderQueue)],
    });
    new events.Rule(this, 'PaymentRefundedRule', {
      eventBus, ruleName: 'order-payment-refunded',
      eventPattern: { source: ['payment-service'], detailType: ['PaymentRefunded'] },
      targets: [new targets.SqsQueue(orderQueue)],
    });

    // ── Payment Event Lambda ─────────────────────────────────────────────────
    const paymentEventFn = new lambda.Function(this, 'PaymentEventFunction', {
      functionName: 'payment-event-service',
      runtime: lambda.Runtime.PYTHON_3_11,
      code: lambda.Code.fromAsset(path.join(__dirname, '../services/payment')),
      handler: 'handler.event_handler',
      layers: [commonLayer],
      logGroup: new logs.LogGroup(this, 'PaymentEventFnLogGroup', {
        logGroupName: '/aws/lambda/payment-event-service',
        retention: logs.RetentionDays.TWO_WEEKS,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        PAYMENTS_TABLE: paymentsTable.tableName,
        IDEMPOTENCY_TABLE: idempotencyTable.tableName,
        EVENT_BUS_NAME: eventBusName,
        PAYMENT_MODE: 'mock',
        STRIPE_SECRET_KEY: 'sk_test_placeholder',
        LOG_LEVEL: 'INFO',
      },
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
    });
    paymentsTable.grantReadWriteData(paymentEventFn);
    idempotencyTable.grantReadWriteData(paymentEventFn);
    eventBus.grantPutEventsTo(paymentEventFn);

    const paymentDlq = new sqs.Queue(this, 'PaymentDLQ', {
      queueName: 'payment-service-dlq',
      retentionPeriod: cdk.Duration.days(14),
    });
    const paymentQueue = new sqs.Queue(this, 'PaymentQueue', {
      queueName: 'payment-service-queue',
      visibilityTimeout: cdk.Duration.seconds(60),
      deadLetterQueue: { queue: paymentDlq, maxReceiveCount: 3 },
    });
    paymentEventFn.addEventSource(new lambdaEventSources.SqsEventSource(paymentQueue, { batchSize: 1 }));

    new events.Rule(this, 'OrderReadyForPaymentRule', {
      eventBus, ruleName: 'payment-order-ready',
      eventPattern: { source: ['order-service'], detailType: ['OrderReadyForPayment'] },
      targets: [new targets.SqsQueue(paymentQueue)],
    });
    new events.Rule(this, 'CompensatePaymentRule', {
      eventBus, ruleName: 'payment-compensate',
      eventPattern: { source: ['order-service'], detailType: ['CompensatePayment'] },
      targets: [new targets.SqsQueue(paymentQueue)],
    });

    // ── SSM exports ──────────────────────────────────────────────────────────
    new ssm.StringParameter(this, 'OrderApiFnNameParam', {
      parameterName: '/ecommerce/order-api-fn-name',
      stringValue: this.orderApiFn.functionName,
    });
    new ssm.StringParameter(this, 'OrderApiFnArnParam', {
      parameterName: '/ecommerce/order-api-fn-arn',
      stringValue: this.orderApiFn.functionArn,
    });

    new cdk.CfnOutput(this, 'OrderApiFunctionName', { value: this.orderApiFn.functionName });
    new cdk.CfnOutput(this, 'OrdersTableName', { value: ordersTable.tableName });
    new cdk.CfnOutput(this, 'PaymentsTableName', { value: paymentsTable.tableName });
  }
}
