"""multi-agent scope: agent_id / project / session_id

Adds the (agent_id, project) ownership scope to memory_chunks and memory_files
so several agents can share one memory service without overwriting each other.

Everything here is additive:
  * new columns are NOT NULL *with a server default*, so the ~2200 pre-existing
    rows are backfilled in place by PostgreSQL and no row is rewritten by hand;
  * the sentinel scope for that pre-existing data is ('legacy', 'default'),
    which is also the default the API applies when a client sends no scope —
    old clients therefore keep addressing exactly the same chunk IDs;
  * memory_files gains a composite primary key (agent_id, project, file_path).
    The old single-column key was itself a cross-agent collision point. The
    existing rows stay unique under the new key because file_path alone was
    already unique.

No table is dropped and no chunk_id is recomputed.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-12

"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in sync with app/scope.py — duplicated deliberately so the migration does
# not import application code.
LEGACY_AGENT_ID = "legacy"
DEFAULT_PROJECT = "default"


def upgrade() -> None:
    # ── memory_chunks ─────────────────────────────────────────────────────────
    op.add_column(
        "memory_chunks",
        sa.Column(
            "agent_id",
            sa.Text(),
            nullable=False,
            server_default=LEGACY_AGENT_ID,
        ),
    )
    op.add_column(
        "memory_chunks",
        sa.Column(
            "project",
            sa.Text(),
            nullable=False,
            server_default=DEFAULT_PROJECT,
        ),
    )
    op.add_column("memory_chunks", sa.Column("session_id", sa.Text(), nullable=True))

    # Explicit backfill. The server_default already populated existing rows, but
    # this makes the intent obvious and makes the migration safe to re-run after
    # a partial failure.
    op.execute(
        sa.text(
            "UPDATE memory_chunks "
            "SET agent_id = :agent WHERE agent_id IS NULL OR agent_id = ''"
        ).bindparams(agent=LEGACY_AGENT_ID)
    )
    op.execute(
        sa.text(
            "UPDATE memory_chunks "
            "SET project = :project WHERE project IS NULL OR project = ''"
        ).bindparams(project=DEFAULT_PROJECT)
    )

    op.create_index("ix_memory_chunks_agent_id", "memory_chunks", ["agent_id"])
    op.create_index("ix_memory_chunks_project", "memory_chunks", ["project"])
    op.create_index(
        "ix_memory_chunks_scope_path",
        "memory_chunks",
        ["agent_id", "project", "file_path"],
    )

    # ── memory_files ──────────────────────────────────────────────────────────
    op.add_column(
        "memory_files",
        sa.Column(
            "agent_id",
            sa.Text(),
            nullable=False,
            server_default=LEGACY_AGENT_ID,
        ),
    )
    op.add_column(
        "memory_files",
        sa.Column(
            "project",
            sa.Text(),
            nullable=False,
            server_default=DEFAULT_PROJECT,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE memory_files "
            "SET agent_id = :agent WHERE agent_id IS NULL OR agent_id = ''"
        ).bindparams(agent=LEGACY_AGENT_ID)
    )
    op.execute(
        sa.text(
            "UPDATE memory_files "
            "SET project = :project WHERE project IS NULL OR project = ''"
        ).bindparams(project=DEFAULT_PROJECT)
    )

    # Widen the primary key from (file_path) to (agent_id, project, file_path).
    op.drop_constraint("memory_files_pkey", "memory_files", type_="primary")
    op.create_primary_key(
        "memory_files_pkey", "memory_files", ["agent_id", "project", "file_path"]
    )
    # file_path is no longer the leading key column; keep it independently
    # indexed for "who else has this path?" lookups.
    op.create_index("ix_memory_files_file_path", "memory_files", ["file_path"])


def downgrade() -> None:
    # Collapsing (agent_id, project, file_path) back to (file_path) is lossy the
    # moment two scopes track the same path: only one row can survive.
    #
    # The previous version of this function silently DELETEd the losers. That is
    # the wrong default for a downgrade — it destroys another agent's file
    # records with no warning and no way to tell what went missing afterwards.
    # Instead: refuse, and report exactly what would be lost.
    #
    # Set ALLOW_LOSSY_DOWNGRADE=1 in the environment to accept the deletion.
    conn = op.get_bind()
    duplicates = conn.execute(
        sa.text(
            """
            SELECT file_path, count(*) AS n
            FROM memory_files
            GROUP BY file_path
            HAVING count(*) > 1
            ORDER BY n DESC, file_path
            LIMIT 20
            """
        )
    ).fetchall()

    if duplicates and os.environ.get("ALLOW_LOSSY_DOWNGRADE") != "1":
        listed = "\n".join(f"    {row.n} scopes -> {row.file_path}" for row in duplicates)
        raise RuntimeError(
            "Refusing to downgrade 0002: "
            f"{len(duplicates)} file_path(s) are tracked by more than one scope, "
            "and the pre-0002 schema can only keep one row per path.\n"
            f"{listed}\n"
            "Resolve them first (export or delete the scopes you do not want to "
            "keep), or re-run with ALLOW_LOSSY_DOWNGRADE=1 to delete all but the "
            "lowest (agent_id, project) for each path."
        )

    if duplicates:
        # Explicitly opted in. Keep the legacy scope where present, otherwise the
        # lexicographically lowest (agent_id, project).
        op.execute(
            sa.text(
                """
                DELETE FROM memory_files a
                USING memory_files b
                WHERE a.file_path = b.file_path
                  AND (a.agent_id, a.project) <> (b.agent_id, b.project)
                  AND b.agent_id = :agent
                  AND a.agent_id <> :agent
                """
            ).bindparams(agent=LEGACY_AGENT_ID)
        )
        op.execute(
            """
            DELETE FROM memory_files a
            USING memory_files b
            WHERE a.file_path = b.file_path
              AND (a.agent_id, a.project) > (b.agent_id, b.project)
            """
        )

    op.drop_index("ix_memory_files_file_path", table_name="memory_files")
    op.drop_constraint("memory_files_pkey", "memory_files", type_="primary")
    op.create_primary_key("memory_files_pkey", "memory_files", ["file_path"])
    op.drop_column("memory_files", "project")
    op.drop_column("memory_files", "agent_id")

    op.drop_index("ix_memory_chunks_scope_path", table_name="memory_chunks")
    op.drop_index("ix_memory_chunks_project", table_name="memory_chunks")
    op.drop_index("ix_memory_chunks_agent_id", table_name="memory_chunks")
    op.drop_column("memory_chunks", "session_id")
    op.drop_column("memory_chunks", "project")
    op.drop_column("memory_chunks", "agent_id")
