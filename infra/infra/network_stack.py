from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
)
from constructs import Construct


class NetworkStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(self, "Vpc", max_azs=2)

        self.alb = elbv2.ApplicationLoadBalancer(
            self, "ALB",
            vpc=self.vpc,
            internet_facing=True,
        )

        self.product_target_group = elbv2.ApplicationTargetGroup(
            self, "ProductTargetGroup",
            vpc=self.vpc,
            port=8080,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/health",
                healthy_http_codes="200",
            ),
        )

        self.cart_target_group = elbv2.ApplicationTargetGroup(
            self, "CartTargetGroup",
            vpc=self.vpc,
            port=8080,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/health",
                healthy_http_codes="200",
            ),
        )

        # Listener on port 80 → product service
        self.alb.add_listener(
            "ProductListener",
            port=80,
            default_target_groups=[self.product_target_group],
        )

        # Listener on port 8081 → cart service
        self.alb.add_listener(
            "CartListener",
            port=8081,
            default_target_groups=[self.cart_target_group],
        )
