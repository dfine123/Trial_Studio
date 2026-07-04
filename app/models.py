"""SQLAlchemy models — User, Clip, Segment, Audio.

User-scoped throughout (no auth in V1, but everything keyed by user_id so auth is
additive later). Segments are a first-class table (the sequencer queries them
directly by energy/duration/vibe in later phases), never nested JSON.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    handle: Mapped[str | None] = mapped_column(String(255))
    voice_label: Mapped[str | None] = mapped_column(String(64))   # display name of this profile's VOICE (default: handle)
    description: Mapped[str | None] = mapped_column(Text)
    niche: Mapped[str | None] = mapped_column(String(255))
    edge_pref: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    clips: Mapped[list["Clip"]] = relationship(back_populates="user")


class ClipFolder(Base):
    """A folder for organizing clips. Nestable via parent_id (sub-folders). Deleting a folder
    cascades to its sub-folders; clips in deleted folders are unfiled (folder_id -> NULL)."""
    __tablename__ = "clip_folders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clip_folders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    r2_key: Mapped[str | None] = mapped_column(String(1024))
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clip_folders.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # uploaded | indexing | indexed | rejected
    status: Mapped[str] = mapped_column(String(32), default="uploaded", index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    # Probe / QC
    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    bitrate: Mapped[int | None] = mapped_column(Integer)

    # Twelve Labs + clip-level index record
    twelvelabs_video_id: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str | None] = mapped_column(Text)
    setting: Mapped[str | None] = mapped_column(String(255))
    lighting_tags: Mapped[list | None] = mapped_column(JSONB)
    vibe_tags: Mapped[list | None] = mapped_column(JSONB)
    time_of_day: Mapped[str | None] = mapped_column(String(64))
    color_temp_k: Mapped[float | None] = mapped_column(Float)
    avg_luminance: Mapped[float | None] = mapped_column(Float)
    dominant_palette: Mapped[list | None] = mapped_column(JSONB)
    camera_movement: Mapped[str | None] = mapped_column(String(64))
    has_speech: Mapped[bool | None] = mapped_column(Boolean)
    has_music: Mapped[bool | None] = mapped_column(Boolean)
    quality_flags: Mapped[list | None] = mapped_column(JSONB)
    embedding: Mapped[list | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="clips")
    segments: Mapped[list["Segment"]] = relationship(
        back_populates="clip",
        cascade="all, delete-orphan",
        order_by="Segment.idx",
    )


class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    clip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clips.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer)  # order within the clip

    start_ts: Mapped[float] = mapped_column(Float)
    end_ts: Mapped[float] = mapped_column(Float)
    duration: Mapped[float] = mapped_column(Float)

    description: Mapped[str | None] = mapped_column(Text)
    motion_intensity: Mapped[float | None] = mapped_column(Float)
    energy: Mapped[float | None] = mapped_column(Float)       # 0..1
    shot_scale: Mapped[str | None] = mapped_column(String(64))
    lighting: Mapped[str | None] = mapped_column(String(128))
    luminance: Mapped[float | None] = mapped_column(Float)
    color_temp_k: Mapped[float | None] = mapped_column(Float)
    subject_bbox: Mapped[dict | None] = mapped_column(JSONB)
    usability_score: Mapped[float | None] = mapped_column(Float)  # 0..1
    is_hero: Mapped[bool] = mapped_column(Boolean, default=False)

    clip: Mapped["Clip"] = relationship(back_populates="segments")


class Audio(Base):
    __tablename__ = "audios"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    # Nullable: starter/library audios may not belong to a user.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    r2_key: Mapped[str | None] = mapped_column(String(1024))
    source: Mapped[str] = mapped_column(String(32), default="upload")
    description: Mapped[str | None] = mapped_column(Text)

    bpm: Mapped[float | None] = mapped_column(Float)
    duration: Mapped[float | None] = mapped_column(Float)
    beat_map: Mapped[list | None] = mapped_column(JSONB)       # float[] seconds (librosa)
    has_core_beat_drop: Mapped[bool] = mapped_column(Boolean, default=False)
    beat_drop_ts: Mapped[float | None] = mapped_column(Float)  # manual pivot (V1)
    structure: Mapped[str | None] = mapped_column(String(32))  # steady | before_after | build_up
    thematic_tags: Mapped[list | None] = mapped_column(JSONB)
    energy_arc: Mapped[str | None] = mapped_column(String(64))
    ig_audio_url: Mapped[str | None] = mapped_column(String(1024))  # null until V2

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DriveConnection(Base):
    """A profile's connected Google Drive folder (read-only). The creator shares a folder with the
    service-account email; we poll it and ingest new videos. One profile can have several."""
    __tablename__ = "drive_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(16), default="gdrive")
    folder_id: Mapped[str] = mapped_column(String(255))
    folder_name: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="connected")  # connected | syncing | error
    last_error: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SyncedFile(Base):
    """Dedup ledger: one row per Drive file we've seen, so re-syncs are incremental and the UI can
    show imported / rejected / failed."""
    __tablename__ = "synced_files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("drive_connections.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    provider_file_id: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16))        # synced | rejected | failed
    reason: Mapped[str | None] = mapped_column(Text)
    clip_ids: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Template(Base):
    """A reusable progression template: a beat-segmented recipe + an LLM-authored Formula Object.

    Authored once (profile-agnostic) in the CapCut studio; instantiated per creator by re-matching
    their clips to the segment tiers and REGENERATING the caption arc in the template's voice. The
    template is never replayed mechanically — `spec.formula` is the inspectable why/how the LLM
    reasons over. `spec` is the dual record validated by `app.templates.spec.TemplateSpec`.
    """
    __tablename__ = "templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    audio_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("audios.id", ondelete="SET NULL"), nullable=True
    )
    spec: Mapped[dict] = mapped_column(JSONB)   # dual record: segments + caption_slots + formula + constraints
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
