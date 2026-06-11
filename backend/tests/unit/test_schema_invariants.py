"""Schema-level invariants.

These are cheap, run without a database, and each one guards a bug that is
invisible until production: a partial index that silently matches nothing, an
unnamed constraint that cannot be dropped, a model Alembic cannot see.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from crucible.db.models import Base

DIALECT = postgresql.dialect()
TABLES = list(Base.metadata.sorted_tables)


def test_every_table_compiles_to_postgres_ddl() -> None:
    for table in TABLES:
        str(CreateTable(table).compile(dialect=DIALECT))


def test_every_index_compiles_to_postgres_ddl() -> None:
    for table in TABLES:
        for index in table.indexes:
            str(CreateIndex(index).compile(dialect=DIALECT))


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_enum_columns_store_lowercase_values(table) -> None:
    """SQLAlchemy stores enum *names* by default -- we require *values*.

    Regression guard: without values_callable, SessionStatus.ACTIVE persists as
    'ACTIVE', and every hand-written predicate comparing to 'active' matches
    nothing at all -- silently.
    """
    for column in table.columns:
        if isinstance(column.type, SAEnum):
            assert all(v == v.lower() for v in column.type.enums), (
                f"{table.name}.{column.name} would store uppercase names: {column.type.enums}"
            )


def test_partial_index_predicates_reference_real_enum_literals() -> None:
    """A predicate like WHERE status = 'actve' is valid SQL and always false."""
    problems: list[str] = []
    for table in TABLES:
        col_types = {c.name: c.type for c in table.columns}
        for index in table.indexes:
            sql = str(CreateIndex(index).compile(dialect=DIALECT))
            if "WHERE" not in sql:
                continue
            predicate = sql.split("WHERE", 1)[1]

            for col, literal in re.findall(r"(\w+)\s*(?:=|<>)\s*'([^']+)'", predicate):
                col_type = col_types.get(col)
                if isinstance(col_type, SAEnum) and literal not in col_type.enums:
                    problems.append(f"{table.name}.{col}: '{literal}' not in {col_type.enums}")

            for col, group in re.findall(r"(\w+) IN \(([^)]*)\)", predicate):
                col_type = col_types.get(col)
                if isinstance(col_type, SAEnum):
                    for literal in re.findall(r"'([^']+)'", group):
                        if literal not in col_type.enums:
                            problems.append(
                                f"{table.name}.{col}: '{literal}' not in {col_type.enums}"
                            )
    assert not problems, "index predicates that can never match:\n" + "\n".join(problems)


def test_all_constraints_are_named() -> None:
    """Unnamed constraints make migrations one-way -- a downgrade cannot drop them."""
    unnamed: list[str] = []
    for table in TABLES:
        for constraint in table.constraints:
            if constraint.name is None:
                unnamed.append(f"{table.name}: {type(constraint).__name__}")
    assert not unnamed, unnamed


def test_every_foreign_key_declares_ondelete() -> None:
    """An FK without ON DELETE leaves deletion semantics to chance."""
    missing: list[str] = []
    for table in TABLES:
        for fk in table.foreign_keys:
            if fk.ondelete is None:
                missing.append(f"{table.name}.{fk.parent.name} -> {fk.target_fullname}")
    assert not missing, "foreign keys with no ON DELETE policy:\n" + "\n".join(missing)


def test_models_package_exports_every_mapped_table() -> None:
    """Alembic autogenerate only sees models that were imported."""
    import crucible.db.models as models

    exported = {
        getattr(models, name).__tablename__
        for name in models.__all__
        if hasattr(getattr(models, name), "__tablename__")
    }
    declared = {t.name for t in TABLES}
    assert declared == exported, f"not exported from models/__init__: {declared - exported}"
