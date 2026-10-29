from __future__ import annotations

import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from db.base import NAMING_CONVENTION, Base

# The SQLAlchemy-documented convention, written out literally so that an edit
# to db.base.NAMING_CONVENTION cannot silently update its own expectation.
EXPECTED_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


# T5 ------------------------------------------------------------------------


def test_the_naming_convention_is_the_documented_set() -> None:
    assert dict(NAMING_CONVENTION) == EXPECTED_CONVENTION


def test_the_base_metadata_uses_the_naming_convention() -> None:
    assert dict(Base.metadata.naming_convention) == EXPECTED_CONVENTION


def test_the_base_metadata_holds_exactly_the_app_tables() -> None:
    # Imported here so the assertion does not depend on which test module
    # happened to import the models first.

    assert set(Base.metadata.tables) == {
        "users",
        "projects",
        "project_members",
        "documents",
        "connections",
        "connection_items",
    }


# T6 ------------------------------------------------------------------------


@pytest.fixture
def child_table() -> Table:
    """A child table whose constraints are all left unnamed on purpose.

    A column-level ``unique=True, index=True`` would yield only a unique
    index and no ``uq_`` constraint, so the unique constraint and the index
    are declared explicitly.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Table("parent", metadata, Column("id", Integer, primary_key=True))
    return Table(
        "child",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("parent.id")),
        Column("code", String(20)),
        CheckConstraint("id > 0", name="positive_id"),
        UniqueConstraint("code"),
        Index(None, "code"),
    )


def create_table_sql(table: Table) -> str:
    return str(CreateTable(table).compile(dialect=postgresql.dialect()))


def test_constraints_get_the_conventional_names(child_table: Table) -> None:
    ddl = create_table_sql(child_table)

    assert "CONSTRAINT pk_child PRIMARY KEY (id)" in ddl
    assert "CONSTRAINT fk_child_parent_id_parent FOREIGN KEY(parent_id)" in ddl
    assert "CONSTRAINT uq_child_code UNIQUE (code)" in ddl
    assert "CONSTRAINT ck_child_positive_id CHECK (id > 0)" in ddl


def test_indexes_get_the_conventional_name(child_table: Table) -> None:
    (index,) = child_table.indexes

    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))

    assert "CREATE INDEX ix_child_code ON child (code)" in ddl


def test_the_referenced_table_gets_a_conventional_primary_key_name(
    child_table: Table,
) -> None:
    parent = child_table.metadata.tables["parent"]

    assert "CONSTRAINT pk_parent PRIMARY KEY (id)" in create_table_sql(parent)
