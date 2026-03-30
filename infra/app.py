#!/usr/bin/env python3
import os

import aws_cdk as cdk

from infra.dynamo_stack import DynamoStack
from infra.network_stack import NetworkStack
from infra.product_service_stack import ProductServiceStack
from infra.cart_service_stack import CartServiceStack


app = cdk.App()

env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=os.getenv("CDK_DEFAULT_REGION"),
)

dynamo_stack = DynamoStack(app, "DynamoStack", env=env)
network_stack = NetworkStack(app, "NetworkStack", env=env)
product_stack = ProductServiceStack(app, "ProductServiceStack", network_stack=network_stack, dynamo_stack=dynamo_stack, env=env)
cart_stack = CartServiceStack(app, "CartServiceStack", network_stack=network_stack, env=env)

product_stack.add_dependency(dynamo_stack)
product_stack.add_dependency(network_stack)
cart_stack.add_dependency(network_stack)

app.synth()
