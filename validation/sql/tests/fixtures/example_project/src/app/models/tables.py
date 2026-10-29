"""Example models matching `sql-schema.json` table for table, column for column."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, column
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customer"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    orders: Mapped[list[CustomerOrder]] = relationship(back_populates="customer", lazy="raise")


class CustomerOrder(Base):
    __tablename__ = "customer_order"
    __table_args__ = (
        Index(
            "uq_customer_order_open_external_ref",
            "external_ref",
            unique=True,
            sqlite_where=column("status") != "CLOSED",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customer.id"))
    status: Mapped[str] = mapped_column(
        Enum("OPEN", "PAID", "CLOSED", name="order_status", native_enum=False), index=True
    )
    external_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    customer: Mapped[Customer] = relationship(back_populates="orders", lazy="selectin")


class CacheEntry(Base):
    __tablename__ = "cache_entry"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
