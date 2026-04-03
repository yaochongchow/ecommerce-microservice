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

export class ShippingStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const eventBusArn = ssm.StringParameter.valueForStringParameter(this, '/ecommerce/event-bus-arn');
    const eventBusName = ssm.StringParameter.valueForStringParameter(this, '/ecommerce/event-bus-name');
    const eventBus = events.EventBus.fromEventBusArn(this, 'SharedBus', eventBusArn);

    const commonLayer = new lambda.LayerVersion(this, 'CommonLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../layers/common')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'Shared utilities (event_utils, logger, responses)',
    });

    const shipmentsTable = new dynamodb.Table(this, 'ShipmentsTable', {
      tableName: 'ShipmentsTable',
      partitionKey: { name: 'shipmentId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    shipmentsTable.addGlobalSecondaryIndex({
      indexName: 'orderId-index',
      partitionKey: { name: 'orderId', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    const shippingFn = new lambda.Function(this, 'ShippingFunction', {
      functionName: 'shipping-service',
      runtime: lambda.Runtime.PYTHON_3_11,
      code: lambda.Code.fromAsset(path.join(__dirname, '../services/shipping')),
      handler: 'handler.lambda_handler',
      layers: [commonLayer],
      logGroup: new logs.LogGroup(this, 'ShippingFnLogGroup', {
        logGroupName: '/aws/lambda/shipping-service',
        retention: logs.RetentionDays.TWO_WEEKS,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        SHIPMENTS_TABLE_NAME: shipmentsTable.tableName,
        EVENT_BUS_NAME: eventBusName,
        LOG_LEVEL: 'INFO',
      },
      timeout: cdk.Duration.seconds(30),
    });

    shipmentsTable.grantReadWriteData(shippingFn);
    eventBus.grantPutEventsTo(shippingFn);

    const shippingDlq = new sqs.Queue(this, 'ShippingDLQ', {
      queueName: 'shipping-service-dlq',
      retentionPeriod: cdk.Duration.days(14),
    });

    const shippingQueue = new sqs.Queue(this, 'ShippingQueue', {
      queueName: 'shipping-service-queue',
      visibilityTimeout: cdk.Duration.seconds(60),
      deadLetterQueue: { queue: shippingDlq, maxReceiveCount: 3 },
    });

    shippingFn.addEventSource(new lambdaEventSources.SqsEventSource(shippingQueue, {
      batchSize: 1,
    }));

    new events.Rule(this, 'OrderConfirmedRule', {
      eventBus,
      ruleName: 'shipping-order-confirmed',
      eventPattern: { source: ['order-service'], detailType: ['OrderConfirmed'] },
      targets: [new targets.SqsQueue(shippingQueue)],
    });

    new cdk.CfnOutput(this, 'ShipmentsTableName', { value: shipmentsTable.tableName });
    new cdk.CfnOutput(this, 'ShippingFunctionName', { value: shippingFn.functionName });
    new cdk.CfnOutput(this, 'ShippingQueueUrl', { value: shippingQueue.queueUrl });
    new cdk.CfnOutput(this, 'ShippingDLQUrl', { value: shippingDlq.queueUrl });
  }
}
