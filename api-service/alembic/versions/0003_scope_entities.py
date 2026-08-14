"""multi-agent scope for entities

Completes the scoping started in 0002. `entities` was left unscoped there
because no code path writes it yet — but an unscoped `entities` table is the
same class of bug 0002 fixed for `memory_chunks`: the first agent to record the
name "Redis" would own that row globally, and every other agent's mentions
would silently attach to it.

Both `entities` and `entity_mentions` are empty at the time of writing (0 rows),
so the unique constraint added here cannot fail on existing data. Adding it
later, once entity extraction is live, would mean de-duplicating in place.

`entity_mentions` intentionally gains no columns: chunk_id already encodes the
scope in its hash and entity_id is now unique per scope, so a mention cannot
span two scopes.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in sync with app/scope.py — duplicated so the migration does not import
# application code (same convention as 0002).
LEGACY_AGENT_ID = "legacy"
DEFAULT_PROJECT = "default"


def upgrade() -> None:
    op.add_column(
        "entities",
        sa.Column("agent_id", sa.Text(), nullable=False, server_default=LEGACY_AGENT_ID),
    )
    op.add_column(
        "entities",
        sa.Column("project", sa.Text(), nullable=False, server_default=DEFAULT_PROJECT),
    )

    # Explicit backfill: a no-op on an empty table, but keeps the migration
    # correct (and re-runnable) if rows appear before it is applied elsewhere.
    op.execute(
        sa.text(
            "UPDATE entities SET agent_id = :agent "
            "WHERE agent_id IS NULL OR agent_id = ''"
        ).bindparams(agent=LEGACY_AGENT_ID)
    )
    op.execute(
        sa.text(
            "UPDATE entities SET project = :project "
            "WHERE project IS NULL OR project = ''"
        ).bindparams(project=DEFAULT_PROJECT)
    )

    op.create_index("ix_entities_scope", "entities", ["agent_id", "project"])
    op.create_unique_constraint(
        "uq_entities_scope_name", "entities", ["agent_id", "project", "name"]
    )


def downgrade() -> None:
    # Unlike 0002 this direction is safe: dropping the scope columns cannot
    # collide, because removing a UNIQUE constraint never conflicts. Duplicate
    # names across scopes simply become duplicate rows again.
    op.drop_constraint("uq_entities_scope_name", "entities", type_="unique")
    op.drop_index("ix_entities_scope", table_name="entities")
    op.drop_column("entities", "project")
    op.drop_column("entities", "agent_id")
