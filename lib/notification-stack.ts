import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as path from 'path';

export class NotificationStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const eventBusArn = ssm.StringParameter.valueForStringParameter(this, '/ecommerce/event-bus-arn');
    const eventBus = events.EventBus.fromEventBusArn(this, 'SharedBus', eventBusArn);

    const commonLayer = new lambda.LayerVersion(this, 'CommonLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../layers/common')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'Shared utilities (event_utils, logger, responses)',
    });

    const notificationFn = new lambda.Function(this, 'NotificationFunction', {
      functionName: 'notification-service',
      runtime: lambda.Runtime.PYTHON_3_11,
      code: lambda.Code.fromAsset(path.join(__dirname, '../services/notification')),
      handler: 'handler.lambda_handler',
      layers: [commonLayer],
      environment: {
        EMAIL_MODE: 'mock',
        LOG_LEVEL: 'INFO',
      },
      timeout: cdk.Duration.seconds(30),
    });

    notificationFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ses:SendEmail', 'ses:SendRawEmail'],
      resources: ['*'],
    }));

    const notificationDlq = new sqs.Queue(this, 'NotificationDLQ', {
      queueName: 'notification-service-dlq',
      retentionPeriod: cdk.Duration.days(14),
    });

    const notificationQueue = new sqs.Queue(this, 'NotificationQueue', {
      queueName: 'notification-service-queue',
      visibilityTimeout: cdk.Duration.seconds(60),
      deadLetterQueue: { queue: notificationDlq, maxReceiveCount: 3 },
    });

    notificationFn.addEventSource(new lambdaEventSources.SqsEventSource(notificationQueue, {
      batchSize: 1,
    }));

    new events.Rule(this, 'PaymentSucceededRule', {
      eventBus,
      ruleName: 'notification-payment-succeeded',
      eventPattern: { source: ['payment-service'], detailType: ['PaymentSucceeded'] },
      targets: [new targets.SqsQueue(notificationQueue)],
    });

    new events.Rule(this, 'ShipmentCreatedRule', {
      eventBus,
      ruleName: 'notification-shipment-created',
      eventPattern: { source: ['shipping-service'], detailType: ['ShipmentCreated'] },
      targets: [new targets.SqsQueue(notificationQueue)],
    });

    new events.Rule(this, 'OrderCanceledRule', {
      eventBus,
      ruleName: 'notification-order-canceled',
      eventPattern: { source: ['order-service'], detailType: ['OrderCanceled'] },
      targets: [new targets.SqsQueue(notificationQueue)],
    });

    new cdk.CfnOutput(this, 'NotificationFunctionName', { value: notificationFn.functionName });
    new cdk.CfnOutput(this, 'NotificationQueueUrl', { value: notificationQueue.queueUrl });
    new cdk.CfnOutput(this, 'NotificationDLQUrl', { value: notificationDlq.queueUrl });
  }
}
