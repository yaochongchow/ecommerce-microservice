from aws_cdk import (
    Stack,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_s3 as s3,
    aws_sqs as sqs,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
)
from constructs import Construct
from .dynamo_stack import DynamoStack
from .network_stack import NetworkStack


class ProductServiceStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, network_stack: NetworkStack, dynamo_stack: DynamoStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Public S3 bucket for product images
        image_bucket = s3.Bucket(
            self, "ProductImagesBucket",
            public_read_access=True,
            block_public_access=s3.BlockPublicAccess(
                block_public_acls=False,
                block_public_policy=False,
                ignore_public_acls=False,
                restrict_public_buckets=False,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # SQS queue for inbound inventory events
        queue = sqs.Queue(self, "InventoryEventsQueue")

        # EventBridge rules routing inventory events to the queue
        event_bus = events.EventBus.from_event_bus_name(self, "DefaultBus", "default")

        for detail_type in ["LowStock", "OutOfStock", "StockReplenished", "ProductRestockedFailed", "InventoryInitialized"]:
            events.Rule(
                self, f"Rule{detail_type}",
                event_bus=event_bus,
                event_pattern=events.EventPattern(
                    source=["inventory-service"],
                    detail_type=[detail_type],
                ),
                targets=[targets.SqsQueue(queue)],
            )

        cluster = ecs.Cluster(self, "Cluster", vpc=network_stack.vpc)

        task_definition = ecs.FargateTaskDefinition(
            self, "TaskDef",
            cpu=256,
            memory_limit_mib=512,
        )

        task_definition.add_container(
            "ProductContainer",
            image=ecs.ContainerImage.from_asset("../src/product_service"),
            port_mappings=[ecs.PortMapping(container_port=8080)],
            environment={
                "AWS_REGION": self.region,
                "PRODUCTS_TABLE": dynamo_stack.products_table.table_name,
                "SQS_QUEUE_URL": queue.queue_url,
                "IMAGE_BUCKET": image_bucket.bucket_name,
            },
            logging=ecs.LogDrivers.aws_logs(stream_prefix="product-service"),
        )

        service = ecs.FargateService(
            self, "Service",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=1,
        )

        service.attach_to_application_target_group(network_stack.product_target_group)
        service.connections.allow_from(network_stack.alb, ec2.Port.tcp(8080))

        dynamo_stack.products_table.grant_read_write_data(task_definition.task_role)
        image_bucket.grant_read_write(task_definition.task_role)
        queue.grant_consume_messages(task_definition.task_role)

        # Allow task to emit events to EventBridge
        task_definition.task_role.add_to_policy(iam.PolicyStatement(
            actions=["events:PutEvents"],
            resources=["*"],
        ))
