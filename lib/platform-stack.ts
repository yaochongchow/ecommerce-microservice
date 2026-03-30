import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as apigwIntegrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as apigwAuthorizers from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import * as cf from 'aws-cdk-lib/aws-cloudfront';
import * as cfOrigins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';
import * as cwActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as ddb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as eventsTargets from 'aws-cdk-lib/aws-events-targets';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';

export class PlatformStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── 1. Lambda helper — defined first so everything below can use it ───────
    const makeFn = (
      id: string,
      folder: string,
      logGroupName: string,
      env: Record<string, string>,
      memorySize = 512,
    ) => {
      const logGroup = new logs.LogGroup(this, `${id}LogGroup`, {
        logGroupName,
        retention: logs.RetentionDays.TWO_WEEKS,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
      return new lambda.Function(this, id, {
        runtime: lambda.Runtime.NODEJS_20_X,
        code: lambda.Code.fromAsset(`lambda/${folder}`),
        handler: 'index.handler',
        memorySize,
        timeout: cdk.Duration.seconds(29),
        tracing: lambda.Tracing.ACTIVE,
        logGroup,
        layers: [sharedLayer],
        environment: { ...env, LOG_LEVEL: 'INFO' },
      });
    };

    // ── 2. Shared Lambda layer ────────────────────────────────────────────────
    const sharedLayer = new lambda.LayerVersion(this, 'SharedLayer', {
      code: lambda.Code.fromAsset('lambda/layers/shared'),
      compatibleRuntimes: [lambda.Runtime.NODEJS_20_X],
      description: 'Shared utilities: logger, event publisher, error types',
    });

    // ── 3. S3 + CloudFront ────────────────────────────────────────────────────
    const frontendBucket = new s3.Bucket(this, 'FrontendBucket', {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
    });

    const oac = new cf.S3OriginAccessControl(this, 'OAC');
    const distribution = new cf.Distribution(this, 'Distribution', {
      defaultBehavior: {
        origin: cfOrigins.S3BucketOrigin.withOriginAccessControl(frontendBucket, { originAccessControl: oac }),
        viewerProtocolPolicy: cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cf.CachePolicy.CACHING_OPTIMIZED,
        compress: true,
      },
      defaultRootObject: 'index.html',
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 200, responsePagePath: '/index.html' },
        { httpStatus: 404, responseHttpStatus: 200, responsePagePath: '/index.html' },
      ],
    });

    new s3deploy.BucketDeployment(this, 'FrontendDeploy', {
      sources: [s3deploy.Source.asset('frontend')],
      destinationBucket: frontendBucket,
      distribution,
      distributionPaths: ['/*'],
    });

    // ── 4. Cognito ────────────────────────────────────────────────────────────
    const userPool = new cognito.UserPool(this, 'UserPool', {
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: false,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const userPoolClient = userPool.addClient('WebClient', {
      authFlows: { userSrp: true, userPassword: true },
      generateSecret: false,
      accessTokenValidity:  cdk.Duration.hours(1),
      idTokenValidity:      cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
    });

    // ── 5. DynamoDB tables ────────────────────────────────────────────────────
    const tableDefaults = {
      billingMode: ddb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    };

    const usersTable = new ddb.Table(this, 'UsersTable', {
      ...tableDefaults,
      partitionKey: { name: 'userId', type: ddb.AttributeType.STRING },
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
    });
    usersTable.addGlobalSecondaryIndex({
      indexName: 'email-index',
      partitionKey: { name: 'email', type: ddb.AttributeType.STRING },
      projectionType: ddb.ProjectionType.ALL,
    });

    const cartsTable = new ddb.Table(this, 'CartsTable', {
      ...tableDefaults,
      partitionKey: { name: 'userId', type: ddb.AttributeType.STRING },
      timeToLiveAttribute: 'expiresAt',
    });

    const orderRefTable = new ddb.Table(this, 'OrderRefTable', {
      ...tableDefaults,
      partitionKey: { name: 'userId',  type: ddb.AttributeType.STRING },
      sortKey:      { name: 'orderId', type: ddb.AttributeType.STRING },
    });

    const sessionsTable = new ddb.Table(this, 'SessionsTable', {
      ...tableDefaults,
      partitionKey: { name: 'sessionId', type: ddb.AttributeType.STRING },
      timeToLiveAttribute: 'expiresAt',
    });

    // ── 6. Shared EventBridge bus (from SharedStack via SSM) + SQS DLQ ───────
    const dlq = new sqs.Queue(this, 'EventDLQ', {
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });

    const eventBusArn = ssm.StringParameter.valueForStringParameter(this, '/ecommerce/event-bus-arn');
    const eventBusName = ssm.StringParameter.valueForStringParameter(this, '/ecommerce/event-bus-name');
    const eventBus = events.EventBus.fromEventBusArn(this, 'SharedBus', eventBusArn);

    // ── 7. Alarms ─────────────────────────────────────────────────────────────
    const alertsTopic = new sns.Topic(this, 'AlertsTopic');

    new cw.Alarm(this, 'DLQAlarm', {
      metric: dlq.metricApproximateNumberOfMessagesVisible(),
      threshold: 1,
      evaluationPeriods: 1,
      alarmDescription: 'Events failing delivery',
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(new cwActions.SnsAction(alertsTopic));

    // ── 8. Post-confirmation trigger ──────────────────────────────────────────
    // Runs after Cognito sign-up and writes the user row to DynamoDB
    const postConfirmFn = makeFn('PostConfirmFn', 'post-confirm', '/aws/lambda/ecomm-post-confirm', {
      USERS_TABLE: usersTable.tableName,
    });
    usersTable.grantWriteData(postConfirmFn);
    userPool.addTrigger(cognito.UserPoolOperation.POST_CONFIRMATION, postConfirmFn);

    // ── 9. User service Lambda ────────────────────────────────────────────────
    const userFn = makeFn('UserFn', 'user-service', '/aws/lambda/ecomm-user-service', {
      USERS_TABLE:             usersTable.tableName,
      CARTS_TABLE:             cartsTable.tableName,
      ORDER_REF_TABLE:         orderRefTable.tableName,
      SESSIONS_TABLE:          sessionsTable.tableName,
      EVENT_BUS_NAME:          eventBusName,
      POWERTOOLS_SERVICE_NAME: 'user-service',
    });
    [usersTable, cartsTable, orderRefTable, sessionsTable].forEach(t => t.grantReadWriteData(userFn));
    eventBus.grantPutEventsTo(userFn);

    new events.Rule(this, 'OrderEventsRule', {
      eventBus,
      eventPattern: {
        source: ['order-service'],
        detailType: ['OrderCreated', 'OrderConfirmed', 'OrderCanceled'],
      },
      targets: [new eventsTargets.LambdaFunction(userFn, { deadLetterQueue: dlq, retryAttempts: 2 })],
    });

    // ── 10. BFF Lambda ────────────────────────────────────────────────────────
    const orderApiFnName = ssm.StringParameter.valueForStringParameter(this, '/ecommerce/order-api-fn-name');

    const bffFn = makeFn('BffFn', 'bff', '/aws/lambda/ecomm-bff', {
      EVENT_BUS_NAME:          eventBusName,
      USER_POOL_ID:            userPool.userPoolId,
      USER_FN_NAME:            userFn.functionName,
      ORDER_API_FN_NAME:       orderApiFnName,
      POWERTOOLS_SERVICE_NAME: 'bff',
    }, 1024);
    userFn.grantInvoke(bffFn);
    eventBus.grantPutEventsTo(bffFn);

    const orderApiFnArn = ssm.StringParameter.valueForStringParameter(this, '/ecommerce/order-api-fn-arn');
    const orderApiFn = lambda.Function.fromFunctionArn(this, 'OrderApiFn', orderApiFnArn);
    orderApiFn.grantInvoke(bffFn);

    // ── 11. HTTP API Gateway ──────────────────────────────────────────────────
    const httpApi = new apigwv2.HttpApi(this, 'HttpApi', {
      apiName: 'ecomm-api',
      corsPreflight: {
        allowHeaders: ['Authorization', 'Content-Type', 'X-Idempotency-Key', 'X-Correlation-Id'],
        allowMethods: [
          apigwv2.CorsHttpMethod.GET,
          apigwv2.CorsHttpMethod.POST,
          apigwv2.CorsHttpMethod.PUT,
          apigwv2.CorsHttpMethod.DELETE,
          apigwv2.CorsHttpMethod.OPTIONS,
        ],
        allowOrigins: ['*'],
        maxAge: cdk.Duration.hours(1),
      },
    });

    const cfnStage = httpApi.defaultStage?.node.defaultChild as apigwv2.CfnStage;
    cfnStage.defaultRouteSettings = { throttlingBurstLimit: 500, throttlingRateLimit: 100 };

    const jwtAuthorizer = new apigwAuthorizers.HttpJwtAuthorizer(
      'CognitoAuthorizer',
      `https://cognito-idp.${this.region}.amazonaws.com/${userPool.userPoolId}`,
      { jwtAudience: [userPoolClient.userPoolClientId] },
    );

    const bffInt  = new apigwIntegrations.HttpLambdaIntegration('BffInt',  bffFn);
    const userInt = new apigwIntegrations.HttpLambdaIntegration('UserInt', userFn);

    type R = [string, apigwv2.HttpMethod, boolean, 'bff' | 'user'];
    const routes: R[] = [
      ['/health',               apigwv2.HttpMethod.GET,    false, 'bff'],
      ['/api/me',               apigwv2.HttpMethod.GET,    true,  'user'],
      ['/api/me',               apigwv2.HttpMethod.PUT,    true,  'user'],
      ['/api/me/cart',          apigwv2.HttpMethod.GET,    true,  'user'],
      ['/api/me/cart',          apigwv2.HttpMethod.POST,   true,  'user'],
      ['/api/me/cart/{itemId}', apigwv2.HttpMethod.DELETE, true,  'user'],
      ['/api/me/orders',        apigwv2.HttpMethod.GET,    true,  'user'],
      ['/api/products',         apigwv2.HttpMethod.GET,    false, 'bff'],
      ['/api/products/{id}',    apigwv2.HttpMethod.GET,    false, 'bff'],
      ['/api/search',           apigwv2.HttpMethod.GET,    false, 'bff'],
      ['/api/orders',           apigwv2.HttpMethod.POST,   true,  'bff'],
      ['/api/orders/{id}',      apigwv2.HttpMethod.GET,    true,  'bff'],
    ];

    for (const [path, method, auth, target] of routes) {
      httpApi.addRoutes({
        path, methods: [method],
        integration: target === 'user' ? userInt : bffInt,
        ...(auth ? { authorizer: jwtAuthorizer } : {}),
      });
    }

    // ── 12. Lambda alarms ─────────────────────────────────────────────────────
    for (const [fn, name] of [[bffFn, 'BFF'], [userFn, 'User']] as [lambda.Function, string][]) {
      new cw.Alarm(this, `${name}ErrorAlarm`, {
        metric: fn.metricErrors({ period: cdk.Duration.minutes(1) }),
        threshold: 5, evaluationPeriods: 3,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }).addAlarmAction(new cwActions.SnsAction(alertsTopic));

      new cw.Alarm(this, `${name}LatencyAlarm`, {
        metric: fn.metricDuration({ period: cdk.Duration.minutes(1), statistic: 'p99' }),
        threshold: 3000, evaluationPeriods: 3,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }).addAlarmAction(new cwActions.SnsAction(alertsTopic));
    }

    // ── 13. SSM parameters ────────────────────────────────────────────────────
    const platformParams: Record<string, string> = {
      '/ecommerce/user-pool-id': userPool.userPoolId,
      '/ecommerce/user-pool-client-id': userPoolClient.userPoolClientId,
      '/ecommerce/api-url': httpApi.apiEndpoint,
      '/ecommerce/cf-domain': distribution.distributionDomainName,
      '/ecommerce/frontend-bucket': frontendBucket.bucketName,
    };
    for (const [name, value] of Object.entries(platformParams)) {
      new ssm.StringParameter(this, name.replace(/\//g, '_').replace(/^_/, ''), {
        parameterName: name, stringValue: value,
      });
    }

    // ── Outputs ───────────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'ApiUrl',             { value: httpApi.apiEndpoint });
    new cdk.CfnOutput(this, 'CloudFrontDomain',   { value: `https://${distribution.distributionDomainName}` });
    new cdk.CfnOutput(this, 'UserPoolId',         { value: userPool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId',   { value: userPoolClient.userPoolClientId });
    new cdk.CfnOutput(this, 'FrontendBucketName', { value: frontendBucket.bucketName });
  }
}
