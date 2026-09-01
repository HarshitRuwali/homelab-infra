"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── memory_files ─────────────────────────────────────────────────────────
    op.create_table(
        "memory_files",
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=True),
        sa.Column("tags", sa.ARRAY(sa.Text()), nullable=True),
        sa.Column("priority", sa.String(length=16), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("file_path"),
    )

    # ── memory_chunks ─────────────────────────────────────────────────────────
    op.create_table(
        "memory_chunks",
        sa.Column("chunk_id", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=True),
        sa.Column("type", sa.String(length=64), nullable=True),
        sa.Column("tags", sa.ARRAY(sa.Text()), nullable=True),
        sa.Column("priority", sa.String(length=16), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "indexed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("chunk_id"),
    )
    op.create_index("ix_memory_chunks_file_path", "memory_chunks", ["file_path"])
    op.create_index("ix_memory_chunks_type", "memory_chunks", ["type"])

    # ── entities ──────────────────────────────────────────────────────────────
    op.create_table(
        "entities",
        sa.Column(
            "entity_id", sa.Integer(), autoincrement=True, nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=True),
        sa.Column("first_seen", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_seen", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("entity_id"),
    )

    # ── entity_mentions ───────────────────────────────────────────────────────
    op.create_table(
        "entity_mentions",
        sa.Column("chunk_id", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["memory_chunks.chunk_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["entities.entity_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("chunk_id", "entity_id"),
    )


def downgrade() -> None:
    op.drop_table("entity_mentions")
    op.drop_table("entities")
    op.drop_index("ix_memory_chunks_type", table_name="memory_chunks")
    op.drop_index("ix_memory_chunks_file_path", table_name="memory_chunks")
    op.drop_table("memory_chunks")
    op.drop_table("memory_files")
