"""Fixtures for schema_fields.yml: the configured entity is `order`, its fields `status` and
`metadata`."""

from __future__ import annotations

# --- kit.configurable.state-field-written-outside-owner ---


def advance(order: object, new_status: str) -> None:
    # ruleid: kit.configurable.state-field-written-outside-owner
    order.status = new_status


def advance_customer_order(customer_order: object, new_status: str) -> None:
    # ruleid: kit.configurable.state-field-written-outside-owner
    customer_order.status = new_status


def rename_status(customer: object, label: str) -> None:
    # another entity: the variable name does not match the configured entity
    # ok: kit.configurable.state-field-written-outside-owner
    customer.status = label


def read_status(order: object) -> object:
    # ok: kit.configurable.state-field-written-outside-owner
    return order.status


# --- kit.configurable.typed-field-assigned-dict-literal ---


def attach_raw(order: object) -> None:
    # ruleid: kit.configurable.typed-field-assigned-dict-literal
    order.metadata = {"source": "import"}


def attach_typed(order: object, metadata: object) -> None:
    # ok: kit.configurable.typed-field-assigned-dict-literal
    order.metadata = metadata


def attach_other(customer: object) -> None:
    # ok: kit.configurable.typed-field-assigned-dict-literal
    customer.metadata = {"source": "import"}
