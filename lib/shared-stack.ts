import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as events from 'aws-cdk-lib/aws-events';
import * as ssm from 'aws-cdk-lib/aws-ssm';

export class SharedStack extends cdk.Stack {
  public readonly eventBus: events.EventBus;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    this.eventBus = new events.EventBus(this, 'EcommerceEventBus', {
      eventBusName: 'ecommerce-event-bus',
    });

    // Publish bus name/ARN to SSM so each service stack resolves independently
    new ssm.StringParameter(this, 'EventBusNameParam', {
      parameterName: '/ecommerce/event-bus-name',
      stringValue: this.eventBus.eventBusName,
    });

    new ssm.StringParameter(this, 'EventBusArnParam', {
      parameterName: '/ecommerce/event-bus-arn',
      stringValue: this.eventBus.eventBusArn,
    });

    new cdk.CfnOutput(this, 'EventBusName', { value: this.eventBus.eventBusName });
    new cdk.CfnOutput(this, 'EventBusArn', { value: this.eventBus.eventBusArn });
  }
}
