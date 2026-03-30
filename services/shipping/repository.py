import os
import boto3
from boto3.dynamodb.conditions import Key
from common.logger import get_logger

logger = get_logger(__name__)

TABLE_NAME = os.environ.get("SHIPMENTS_TABLE_NAME", "ShipmentsTable")
dynamodb = boto3.resource("dynamodb")


def get_table():
    return dynamodb.Table(TABLE_NAME)


def put_shipment(item: dict):
    get_table().put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(shipmentId)",
    )
    logger.info(f"Stored shipment {item['shipmentId']}")


def get_shipment(shipment_id: str) -> dict | None:
    response = get_table().get_item(Key={"shipmentId": shipment_id})
    return response.get("Item")


def get_shipment_by_order(order_id: str) -> dict | None:
    response = get_table().query(
        IndexName="orderId-index",
        KeyConditionExpression=Key("orderId").eq(order_id),
    )
    items = response.get("Items", [])
    return items[0] if items else None
