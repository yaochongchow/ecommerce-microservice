from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
)
from constructs import Construct
from .network_stack import NetworkStack


class CartServiceStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, network_stack: NetworkStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cluster = ecs.Cluster(self, "Cluster", vpc=network_stack.vpc)

        task_definition = ecs.FargateTaskDefinition(
            self, "TaskDef",
            cpu=256,
            memory_limit_mib=512,
        )

        # Redis sidecar — cart service connects to localhost:6379
        redis_container = task_definition.add_container(
            "Redis",
            image=ecs.ContainerImage.from_registry("redis:alpine"),
            port_mappings=[ecs.PortMapping(container_port=6379)],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="cart-redis"),
        )

        cart_container = task_definition.add_container(
            "CartContainer",
            image=ecs.ContainerImage.from_asset("../src/cart_service"),
            port_mappings=[ecs.PortMapping(container_port=8080)],
            environment={
                "AWS_REGION": self.region,
                "REDIS_ADDR": "localhost:6379",
                "PRODUCT_SERVICE_URL": f"http://{network_stack.alb.load_balancer_dns_name}",
            },
            logging=ecs.LogDrivers.aws_logs(stream_prefix="cart-service"),
        )

        # Ensure Redis starts before the cart service
        cart_container.add_container_dependencies(ecs.ContainerDependency(
            container=redis_container,
            condition=ecs.ContainerDependencyCondition.START,
        ))

        service = ecs.FargateService(
            self, "Service",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=1,
        )

        service.attach_to_application_target_group(network_stack.cart_target_group)
        service.connections.allow_from(network_stack.alb, ec2.Port.tcp(8080))
