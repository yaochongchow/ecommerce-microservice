import os
import boto3
from boto3.dynamodb.conditions import Key
from common.logger import get_logger

logger = get_logger(__name__)

dynamodb = boto3.resource("dynamodb")
dynamodb_client = boto3.client("dynamodb")  # for transact_write

INVENTORY_TABLE_NAME = os.environ.get("INVENTORY_TABLE_NAME", "InventoryTable")
RESERVATIONS_TABLE_NAME = os.environ.get("RESERVATIONS_TABLE_NAME", "ReservationsTable")


def _inventory_table():
    return dynamodb.Table(INVENTORY_TABLE_NAME)


def _reservations_table():
    return dynamodb.Table(RESERVATIONS_TABLE_NAME)


# ── Inventory table ────────────────────────────────────────────────────────────

def put_product(product_id: str, available: int, created_at: str):
    _inventory_table().put_item(
        Item={
            "productId": product_id,
            "available": available,
            "reserved": 0,
            "updatedAt": created_at,
        },
        ConditionExpression="attribute_not_exists(productId)",  # prevent overwriting existing product
    )
    logger.info(f"New product created: {product_id}, initial stock={available}")


def get_inventory(product_id: str) -> dict | None:
    response = _inventory_table().get_item(Key={"productId": product_id})
    return response.get("Item")


def add_stock(product_id: str, quantity: int, updated_at: str):
    _inventory_table().update_item(
        Key={"productId": product_id},
        UpdateExpression="SET available = available + :q, updatedAt = :u",
        ConditionExpression="attribute_exists(productId)",  # product must already exist
        ExpressionAttributeValues={":q": quantity, ":u": updated_at},
    )
    logger.info(f"Stock added for {product_id}: +{quantity}")



def transact_reserve(order_id: str, product_id: str, quantity: int, updated_at: str):
    """Atomically reserve inventory AND write the reservation record in a single transaction.

    Uses DynamoDB TransactWrite so that if Lambda crashes between the two operations
    (inventory decrement + reservation insert), a retry will hit the duplicate condition
    on the reservation put and gracefully skip — no double-deduction of stock.

    Raises TransactionCanceledException with CancellationReasons:
      - reasons[0].Code == "ConditionalCheckFailed" → insufficient stock or product missing
      - reasons[1].Code == "ConditionalCheckFailed" → reservation already exists (duplicate event)
    """
    dynamodb_client.transact_write_items(TransactItems=[
        {
            "Update": {
                "TableName": INVENTORY_TABLE_NAME,
                "Key": {"productId": {"S": product_id}},
                "UpdateExpression": "ADD available :neg_q, reserved :q SET updatedAt = :u",
                "ConditionExpression": "available >= :q AND attribute_exists(productId)",
                "ExpressionAttributeValues": {
                    ":q":     {"N": str(quantity)},
                    ":neg_q": {"N": str(-quantity)},
                    ":u":     {"S": updated_at},
                },
            }
        },
        {
            "Put": {
                "TableName": RESERVATIONS_TABLE_NAME,
                "Item": {
                    "orderId":   {"S": order_id},
                    "productId": {"S": product_id},
                    "quantity":  {"N": str(quantity)},
                    "status":    {"S": "RESERVED"},
                },
                "ConditionExpression": "attribute_not_exists(orderId) AND attribute_not_exists(productId)",
            }
        },
    ])
    logger.info(f"Transactionally reserved {quantity} units of {product_id} for order {order_id}")


def atomic_release(product_id: str, quantity: int, updated_at: str):
    """Atomically add back to available and decrement reserved. Used for rollback
    compensation and order cancellation of RESERVED items.
    """
    _inventory_table().update_item(
        Key={"productId": product_id},
        UpdateExpression="ADD available :q, reserved :neg_q SET updatedAt = :u",
        ExpressionAttributeValues={":q": quantity, ":neg_q": -quantity, ":u": updated_at},
    )
    logger.info(f"Atomically released {quantity} units of {product_id}")


def atomic_fulfill(product_id: str, quantity: int, updated_at: str):
    """Atomically decrement reserved only — available was already deducted at reserve time.
    Used when a shipment is created and items physically leave the warehouse.
    """
    _inventory_table().update_item(
        Key={"productId": product_id},
        UpdateExpression="ADD reserved :neg_q SET updatedAt = :u",
        ExpressionAttributeValues={":neg_q": -quantity, ":u": updated_at},
    )
    logger.info(f"Atomically fulfilled {quantity} units of {product_id}")


def delete_reservation(order_id: str, product_id: str):
    _reservations_table().delete_item(Key={"orderId": order_id, "productId": product_id})
    logger.info(f"Reservation deleted: order={order_id}, product={product_id}")


# ── Reservations table ─────────────────────────────────────────────────────────

def get_reservations_by_order(order_id: str) -> list:
    response = _reservations_table().query(
        KeyConditionExpression=Key("orderId").eq(order_id)
    )
    return response.get("Items", [])


def update_reservation_status(order_id: str, product_id: str, status: str):
    _reservations_table().update_item(
        Key={"orderId": order_id, "productId": product_id},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},  # 'status' is a reserved word in DynamoDB
        ExpressionAttributeValues={":s": status},
    )
    logger.info(f"Reservation status updated: order={order_id}, product={product_id} → {status}")


