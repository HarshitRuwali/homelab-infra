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
from sqlalchemy.orm import relationship

from app.database import Base


class MemoryChunk(Base):
    """One indexed chunk from a markdown memory file."""

    __tablename__ = "memory_chunks"

    chunk_id = Column(Text, primary_key=True)          # sha256(file_path + chunk_index)
    file_path = Column(Text, nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)
    chunk_text = Column(Text)
    type = Column(String(64), index=True)              # project / goal / history / ...
    tags = Column(ARRAY(Text))
    priority = Column(String(16))                      # high / medium / low
    summary = Column(Text)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    indexed_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    mentions = relationship(
        "EntityMention", back_populates="chunk", cascade="all, delete-orphan"
    )


class MemoryFile(Base):
    """One row per tracked markdown file."""

    __tablename__ = "memory_files"

    file_path = Column(Text, primary_key=True)
    type = Column(String(64))
    tags = Column(ARRAY(Text))
    priority = Column(String(16))
    updated_at = Column(TIMESTAMP(timezone=True))
    chunk_count = Column(Integer)


class Entity(Base):
    """Named entity extracted from memory chunks (Phase 8)."""

    __tablename__ = "entities"

    entity_id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    type = Column(String(64))                          # person / project / place / concept
    first_seen = Column(TIMESTAMP(timezone=True))
    last_seen = Column(TIMESTAMP(timezone=True))

    mentions = relationship("EntityMention", back_populates="entity")


class EntityMention(Base):
    """Links entities to the memory chunks they appear in."""

    __tablename__ = "entity_mentions"

    chunk_id = Column(
        Text, ForeignKey("memory_chunks.chunk_id", ondelete="CASCADE"), primary_key=True
    )
    entity_id = Column(
        Integer, ForeignKey("entities.entity_id", ondelete="CASCADE"), primary_key=True
    )

    chunk = relationship("MemoryChunk", back_populates="mentions")
    entity = relationship("Entity", back_populates="mentions")
