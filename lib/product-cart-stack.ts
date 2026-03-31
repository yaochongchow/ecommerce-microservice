import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as events from 'aws-cdk-lib/aws-events';
import * as eventsTargets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as path from 'path';

export class ProductCartStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── VPC ──────────────────────────────────────────────────────────────────
    const vpc = new ec2.Vpc(this, 'ServiceVpc', {
      maxAzs: 2,
      natGateways: 1,
    });

    // ── Products DynamoDB table ──────────────────────────────────────────────
    const productsTable = new dynamodb.Table(this, 'ProductsTable', {
      tableName: 'products',
      partitionKey: { name: 'product_id', type: dynamodb.AttributeType.NUMBER },
      sortKey: { name: 'category', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── S3 bucket for product images (public read) ───────────────────────────
    const imageBucket = new s3.Bucket(this, 'ProductImagesBucket', {
      publicReadAccess: true,
      blockPublicAccess: new s3.BlockPublicAccess({
        blockPublicAcls: false,
        blockPublicPolicy: false,
        ignorePublicAcls: false,
        restrictPublicBuckets: false,
      }),
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ── SQS queue for inbound inventory events ───────────────────────────────
    const inventoryQueue = new sqs.Queue(this, 'InventoryEventsQueue');

    const eventBus = events.EventBus.fromEventBusName(this, 'DefaultBus', 'default');
    for (const detailType of ['LowStock', 'OutOfStock', 'StockReplenished', 'ProductRestockedFailed', 'InventoryInitialized']) {
      new events.Rule(this, `Rule${detailType}`, {
        eventBus,
        eventPattern: {
          source: ['inventory-service'],
          detailType: [detailType],
        },
        targets: [new eventsTargets.SqsQueue(inventoryQueue)],
      });
    }

    // ── ECS Cluster ──────────────────────────────────────────────────────────
    const cluster = new ecs.Cluster(this, 'ServiceCluster', {
      vpc,
      clusterName: 'ecomm-services',
    });

    // ── ALB ──────────────────────────────────────────────────────────────────
    const alb = new elbv2.ApplicationLoadBalancer(this, 'ServiceAlb', {
      vpc,
      internetFacing: true,
    });

    // ── Product Service (Fargate) ────────────────────────────────────────────
    const productTaskDef = new ecs.FargateTaskDefinition(this, 'ProductTask', {
      cpu: 256,
      memoryLimitMiB: 512,
    });
    productTaskDef.addContainer('product', {
      image: ecs.ContainerImage.fromAsset(path.join(__dirname, '../services/product')),
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'product-service' }),
      environment: {
        PRODUCTS_TABLE: productsTable.tableName,
        AWS_REGION: cdk.Stack.of(this).region,
        IMAGE_BUCKET: imageBucket.bucketName,
        SQS_QUEUE_URL: inventoryQueue.queueUrl,
      },
      portMappings: [{ containerPort: 8080 }],
    });
    productsTable.grantReadWriteData(productTaskDef.taskRole);
    imageBucket.grantReadWrite(productTaskDef.taskRole);
    inventoryQueue.grantConsumeMessages(productTaskDef.taskRole);
    productTaskDef.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['events:PutEvents'],
      resources: ['*'],
    }));

    const productService = new ecs.FargateService(this, 'ProductService', {
      cluster,
      taskDefinition: productTaskDef,
      desiredCount: 1,
    });

    const productListener = alb.addListener('ProductListener', { port: 80 });
    productListener.addTargets('ProductTarget', {
      port: 8080,
      targets: [productService],
      healthCheck: { path: '/health', interval: cdk.Duration.seconds(30) },
    });

    // ── Cart Service (Fargate + Redis sidecar) ───────────────────────────────
    const cartTaskDef = new ecs.FargateTaskDefinition(this, 'CartTask', {
      cpu: 256,
      memoryLimitMiB: 512,
    });

    // Redis sidecar — no portMappings to prevent ALB from health-checking Redis
    const redisContainer = cartTaskDef.addContainer('redis', {
      image: ecs.ContainerImage.fromRegistry('redis:alpine'),
      essential: true,
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'cart-redis' }),
    });

    const cartContainer = cartTaskDef.addContainer('cart', {
      image: ecs.ContainerImage.fromAsset(path.join(__dirname, '../services/cart')),
      essential: true,
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'cart-service' }),
      environment: {
        REDIS_ADDR: 'localhost:6379',
        PRODUCT_SERVICE_URL: `http://${alb.loadBalancerDnsName}`,
      },
      portMappings: [{ containerPort: 8080 }],
    });
    cartContainer.addContainerDependencies({
      container: redisContainer,
      condition: ecs.ContainerDependencyCondition.START,
    });

    const cartService = new ecs.FargateService(this, 'CartService', {
      cluster,
      taskDefinition: cartTaskDef,
      desiredCount: 1,
    });
    cartService.connections.allowFrom(alb, ec2.Port.tcp(8080));

    const cartListener = alb.addListener('CartListener', { port: 8081, protocol: elbv2.ApplicationProtocol.HTTP });
    cartListener.addTargets('CartTarget', {
      port: 8080,
      targets: [cartService.loadBalancerTarget({
        containerName: 'cart',
        containerPort: 8080,
      })],
      healthCheck: { path: '/health', interval: cdk.Duration.seconds(30) },
    });

    // ── SSM exports ──────────────────────────────────────────────────────────
    new ssm.StringParameter(this, 'ProductServiceUrlParam', {
      parameterName: '/ecommerce/product-service-url',
      stringValue: `http://${alb.loadBalancerDnsName}`,
    });

    new cdk.CfnOutput(this, 'ALBDnsName', { value: alb.loadBalancerDnsName });
    new cdk.CfnOutput(this, 'ProductServiceUrl', { value: `http://${alb.loadBalancerDnsName}` });
    new cdk.CfnOutput(this, 'CartServiceUrl', { value: `http://${alb.loadBalancerDnsName}:8081` });
    new cdk.CfnOutput(this, 'ProductsTableName', { value: productsTable.tableName });
    new cdk.CfnOutput(this, 'ImageBucketName', { value: imageBucket.bucketName });
  }
}
