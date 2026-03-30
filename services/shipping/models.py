from dataclasses import dataclass


@dataclass
class ShippingAddress:
    name: str
    addressLine1: str
    city: str
    state: str
    zip: str
    country: str
    addressLine2: str = ""


@dataclass
class Shipment:
    shipment_id: str
    order_id: str
    email: str
    carrier: str
    tracking_number: str
    status: str
    shipping_address: dict
    created_at: str
