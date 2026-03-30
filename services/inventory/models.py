from dataclasses import dataclass
from typing import List

@dataclass
class OrderItem:
    product_id: str
    quantity: int

@dataclass
class InventoryRecord:
    product_id: str
    available: int
    reserved: int
    updated_at: str
