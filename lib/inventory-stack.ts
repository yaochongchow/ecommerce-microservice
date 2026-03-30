import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as ssm from "aws-cdk-lib/aws-ssm";
import * as path from "path";

export class InventoryStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const eventBusArn = ssm.StringParameter.valueForStringParameter(
      this,
      "/ecommerce/event-bus-arn",
    );
    const eventBusName = ssm.StringParameter.valueForStringParameter(
      this,
      "/ecommerce/event-bus-name",
    );
    const eventBus = events.EventBus.fromEventBusArn(
      this,
      "SharedBus",
      eventBusArn,
    );

    // Shared utilities layer — each stack owns its own copy pointing to the same source
    const commonLayer = new lambda.LayerVersion(this, "CommonLayer", {
      code: lambda.Code.fromAsset(path.join(__dirname, "../layers/common")),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: "Shared utilities (event_utils, logger, responses)",
    });

    const inventoryTable = new dynamodb.Table(this, "InventoryTable", {
      tableName: "InventoryTable",
      partitionKey: { name: "productId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Tracks per-order reservations so inventory service never needs to query
    // another service's database to know what was reserved or whether items shipped
    const reservationsTable = new dynamodb.Table(this, "ReservationsTable", {
      tableName: "ReservationsTable",
      partitionKey: { name: "orderId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "productId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const inventoryFn = new lambda.Function(this, "InventoryFunction", {
      functionName: "inventory-service",
      runtime: lambda.Runtime.PYTHON_3_11,
      code: lambda.Code.fromAsset(
        path.join(__dirname, "../services/inventory"),
      ),
      handler: "handler.lambda_handler",
      layers: [commonLayer],
      environment: {
        INVENTORY_TABLE_NAME: inventoryTable.tableName,
        RESERVATIONS_TABLE_NAME: reservationsTable.tableName,
        EVENT_BUS_NAME: eventBusName,
        LOW_STOCK_THRESHOLD: "10",
        LOG_LEVEL: "INFO",
      },
      timeout: cdk.Duration.seconds(30),
    });

    inventoryTable.grantReadWriteData(inventoryFn);
    reservationsTable.grantReadWriteData(inventoryFn);
    eventBus.grantPutEventsTo(inventoryFn);

    // DLQ — catches messages that fail after 3 attempts
    const inventoryDlq = new sqs.Queue(this, "InventoryDLQ", {
      queueName: "inventory-service-dlq",
      retentionPeriod: cdk.Duration.days(14),
    });

    // Main queue — buffers all incoming events for the inventory service
    const inventoryQueue = new sqs.Queue(this, "InventoryQueue", {
      queueName: "inventory-service-queue",
      visibilityTimeout: cdk.Duration.seconds(60), // must be >= Lambda timeout
      deadLetterQueue: { queue: inventoryDlq, maxReceiveCount: 3 },
    });

    // Lambda polls the queue (batchSize=1 keeps processing simple and ordered)
    inventoryFn.addEventSource(new lambdaEventSources.SqsEventSource(inventoryQueue, {
      batchSize: 1,
    }));

    // When a new product is created, initialize its inventory record
    new events.Rule(this, "ProductCreatedRule", {
      eventBus,
      ruleName: "inventory-product-created",
      eventPattern: { source: ["product-service"], detailType: ["ProductCreated"] },
      targets: [new targets.SqsQueue(inventoryQueue)],
    });

    // When a product is restocked, add incoming quantity to available
    new events.Rule(this, "ProductRestockedRule", {
      eventBus,
      ruleName: "inventory-product-restocked",
      eventPattern: { source: ["product-service"], detailType: ["ProductRestocked"] },
      targets: [new targets.SqsQueue(inventoryQueue)],
    });

    // When order is created, reserve the items and reduce from available
    new events.Rule(this, "OrderCreatedRule", {
      eventBus,
      ruleName: "inventory-order-created",
      eventPattern: { source: ["order-service"], detailType: ["OrderCreated"] },
      targets: [new targets.SqsQueue(inventoryQueue)],
    });

    // When order is canceled, release the items from reserved, add back to available
    new events.Rule(this, "OrderCanceledRule", {
      eventBus,
      ruleName: "inventory-order-canceled",
      eventPattern: { source: ["order-service"], detailType: ["OrderCanceled"] },
      targets: [new targets.SqsQueue(inventoryQueue)],
    });

    // When items ship, clear them from reserved (they've left the warehouse)
    new events.Rule(this, "ShipmentCreatedRule", {
      eventBus,
      ruleName: "inventory-shipment-created",
      eventPattern: { source: ["shipping-service"], detailType: ["ShipmentCreated"] },
      targets: [new targets.SqsQueue(inventoryQueue)],
    });

    // When items are returned, add them back to available
    new events.Rule(this, "OrderReturnedRule", {
      eventBus,
      ruleName: "inventory-order-returned",
      eventPattern: { source: ["order-service"], detailType: ["OrderReturned"] },
      targets: [new targets.SqsQueue(inventoryQueue)],
    });

    new cdk.CfnOutput(this, "InventoryTableName", { value: inventoryTable.tableName });
    new cdk.CfnOutput(this, "ReservationsTableName", { value: reservationsTable.tableName });
    new cdk.CfnOutput(this, "InventoryFunctionName", { value: inventoryFn.functionName });
    new cdk.CfnOutput(this, "InventoryQueueUrl", { value: inventoryQueue.queueUrl });
    new cdk.CfnOutput(this, "InventoryDLQUrl", { value: inventoryDlq.queueUrl });
  }
}
