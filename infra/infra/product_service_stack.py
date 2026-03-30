from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
)
from constructs import Construct
from .dynamo_stack import DynamoStack
from .network_stack import NetworkStack


class ProductServiceStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, network_stack: NetworkStack, dynamo_stack: DynamoStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

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
