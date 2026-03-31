from aws_cdk import (
    Stack,
    CfnOutput,
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
            protocol=elbv2.ApplicationProtocol.HTTP,
            default_target_groups=[self.cart_target_group],
        )

        dns = self.alb.load_balancer_dns_name

        CfnOutput(self, "ProductServiceURL",
            value=f"http://{dns}",
            description="Product Service base URL (port 80)",
        )
        CfnOutput(self, "CartServiceURL",
            value=f"http://{dns}:8081",
            description="Cart Service base URL (port 8081)",
        )
        CfnOutput(self, "ExampleProductHealth",
            value=f"curl http://{dns}/health",
        )
        CfnOutput(self, "ExampleProductList",
            value=f"curl 'http://{dns}/products/?limit=5'",
        )
        CfnOutput(self, "ExampleProductSearch",
            value=f"curl 'http://{dns}/products/search?q=nike'",
        )
        CfnOutput(self, "ExampleCartHealth",
            value=f"curl http://{dns}:8081/health",
        )
        CfnOutput(self, "ExampleCartCreate",
            value=f"curl -X POST http://{dns}:8081/cart/create/user1",
        )
        CfnOutput(self, "ExampleCartAddItem",
            value=f'curl -X POST http://{dns}:8081/cart/add/user1 -H "Content-Type: application/json" -d \'{{"product_id": 1, "quantity": 2}}\'',
        )
        CfnOutput(self, "ExampleCartPriceCheck",
            value=f"curl http://{dns}:8081/cart/user1/pricecheck",
        )
