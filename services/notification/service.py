from email_client import send_email
from common.logger import get_logger

logger = get_logger(__name__)


def notify_payment_succeeded(detail: dict):
    order_id = detail["orderId"]
    email = detail["email"]
    items = detail.get("items", [])

    item_lines = "\n".join(
        f"  - {i.get('productName', i['productId'])} x{i['quantity']}"
        for i in items
    )
    body = (
        f"Thank you for your order!\n\n"
        f"Order ID: {order_id}\n"
        f"Items:\n{item_lines}\n\n"
        f"Your payment has been processed and your order is being prepared."
    )

    send_email(to=email, subject="Order Confirmation", body=body)
    logger.info(f"Order confirmation sent for {order_id}")


def notify_shipment_created(detail: dict):
    order_id = detail["orderId"]
    email = detail["email"]
    tracking_number = detail["trackingNumber"]
    carrier = detail["carrier"]

    body = (
        f"Your order has shipped!\n\n"
        f"Order ID: {order_id}\n"
        f"Carrier:  {carrier}\n"
        f"Tracking: {tracking_number}\n\n"
        f"You can use the tracking number above to follow your shipment."
    )

    send_email(to=email, subject="Your Order Has Shipped", body=body)
    logger.info(f"Shipment notification sent for order {order_id}, tracking {tracking_number}")


def notify_order_canceled(detail: dict):
    order_id = detail["orderId"]
    email = detail["email"]
    reason = detail.get("reason", "your order has been cancelled")

    body = (
        f"Your order has been cancelled.\n\n"
        f"Order ID: {order_id}\n"
        f"Reason:   {reason}\n\n"
        f"If you paid for this order, a refund will be issued to your original payment method."
    )

    send_email(to=email, subject="Your Order Has Been Cancelled", body=body)
    logger.info(f"Cancellation notification sent for order {order_id}, reason: {reason}")
