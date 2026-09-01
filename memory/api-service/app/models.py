from datetime import datetime

from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
    ARRAY,
    TIMESTAMP,
    func,
)
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base
from app.scope import DEFAULT_PROJECT, LEGACY_AGENT_ID


class MemoryChunk(Base):
    """One indexed chunk from a markdown memory file."""

    __tablename__ = "memory_chunks"

    # sha256(file_path + chunk_index) for the legacy scope,
    # sha256(agent_id + project + file_path + chunk_index) for every other scope.
    chunk_id = Column(Text, primary_key=True)
    file_path = Column(Text, nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)
    chunk_text = Column(Text)
    type = Column(String(64), index=True)              # project / goal / history / ...
    tags = Column(ARRAY(Text))
    priority = Column(String(16))                      # high / medium / low
    summary = Column(Text)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    indexed_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    # ── Multi-agent scope ─────────────────────────────────────────────────────
    # server_default keeps pre-existing rows and raw SQL inserts valid.
    agent_id = Column(
        Text, nullable=False, server_default=LEGACY_AGENT_ID, index=True
    )
    project = Column(
        Text, nullable=False, server_default=DEFAULT_PROJECT, index=True
    )
    session_id = Column(Text, nullable=True)           # metadata only, not part of identity

    __table_args__ = (
        Index("ix_memory_chunks_scope_path", "agent_id", "project", "file_path"),
    )

    mentions = relationship(
        "EntityMention", back_populates="chunk", cascade="all, delete-orphan"
    )


class MemoryFile(Base):
    """One row per tracked markdown file, per scope.

    The primary key is (agent_id, project, file_path): the same path tracked by
    two different agents is two different rows.
    """

    __tablename__ = "memory_files"

    agent_id = Column(Text, primary_key=True, server_default=LEGACY_AGENT_ID)
    project = Column(Text, primary_key=True, server_default=DEFAULT_PROJECT)
    file_path = Column(Text, primary_key=True, index=True)
    type = Column(String(64))
    tags = Column(ARRAY(Text))
    priority = Column(String(16))
    updated_at = Column(TIMESTAMP(timezone=True))
    chunk_count = Column(Integer)


class Entity(Base):
    """Named entity extracted from memory chunks (Phase 8).

    Scoped like MemoryChunk: "Redis" as understood by one agent in one project
    is not automatically the same entity another agent means by that name.
    Without the scope columns, the first writer of a name would own it globally
    and every other agent's mentions would silently attach to it.
    """

    __tablename__ = "entities"

    entity_id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    type = Column(String(64))                          # person / project / place / concept
    first_seen = Column(TIMESTAMP(timezone=True))
    last_seen = Column(TIMESTAMP(timezone=True))

    # ── Multi-agent scope ─────────────────────────────────────────────────────
    agent_id = Column(Text, nullable=False, server_default=LEGACY_AGENT_ID)
    project = Column(Text, nullable=False, server_default=DEFAULT_PROJECT)

    __table_args__ = (
        # One row per name per scope. Both tables are empty today, so this is
        # free to add now and painful to add later.
        UniqueConstraint("agent_id", "project", "name", name="uq_entities_scope_name"),
        Index("ix_entities_scope", "agent_id", "project"),
    )

    mentions = relationship("EntityMention", back_populates="entity")


class EntityMention(Base):
    """Links entities to the memory chunks they appear in.

    Deliberately carries no scope columns of its own: both sides are already
    scoped (chunk_id encodes the scope in its hash, entity_id is unique per
    scope), so a mention cannot span two scopes without one of the foreign
    keys being wrong. ON DELETE CASCADE on both sides keeps it consistent.
    """

    __tablename__ = "entity_mentions"

    chunk_id = Column(
        Text, ForeignKey("memory_chunks.chunk_id", ondelete="CASCADE"), primary_key=True
    )
    entity_id = Column(
        Integer, ForeignKey("entities.entity_id", ondelete="CASCADE"), primary_key=True
    )

    chunk = relationship("MemoryChunk", back_populates="mentions")
    entity = relationship("Entity", back_populates="mentions")
