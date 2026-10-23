"""vector search settings as database defaults

The semantic cache filters its HNSW search by namespace. With pgvector's iterative scans the
index walk continues until it finds rows that pass the filter (instead of returning nothing
when the first `ef_search` candidates all belong to other tenants). Setting these as database
defaults means every session has them, including through a transaction-pooling PgBouncer,
without a SET round trip in front of each lookup.

`plan_cache_mode = force_custom_plan`: the lookup is a prepared statement, and after five runs
Postgres may switch to a generic plan that assumes an average namespace (a handful of rows)
and brute-forces it through the btree index. For a large tenant that is a sequential distance
computation over every row; measured at 6k rows it took ~60 ms instead of 4 ms through HNSW.
Custom plans let the planner choose per namespace: btree for small ones, HNSW for large ones.
The queries are simple, so planning each one costs ~0.1 ms.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTINGS = {
    "hnsw.iterative_scan": "relaxed_order",
    "hnsw.ef_search": "64",
    "plan_cache_mode": "force_custom_plan",
}


def _alter_database(clause: str) -> None:
    op.execute(
        f"DO $$ BEGIN EXECUTE format('ALTER DATABASE %I {clause}', current_database()); END $$"
    )


def upgrade() -> None:
    # Load pgvector's library in this session so its settings are known (and validated).
    op.execute("SELECT '[1]'::vector")
    for name, value in SETTINGS.items():
        _alter_database(f"SET {name} = ''{value}''")


def downgrade() -> None:
    for name in SETTINGS:
        _alter_database(f"RESET {name}")
