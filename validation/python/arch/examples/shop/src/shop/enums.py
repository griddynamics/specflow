"""The enums the wire shapes carry."""

from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    NEW = "NEW"
    PAID = "PAID"
    SHIPPED = "SHIPPED"
    CANCELLED = "CANCELLED"


class UserRole(StrEnum):
    ADMIN = "ADMIN"
    CUSTOMER = "CUSTOMER"
