"""Pydantic response models."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SegmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    idx: int
    start_ts: float
    end_ts: float
    duration: float
    description: str | None
    motion_intensity: float | None
    energy: float | None
    shot_scale: str | None
    lighting: str | None
    luminance: float | None
    color_temp_k: float | None
    subject_bbox: dict | None
    usability_score: float | None
    is_hero: bool


class ClipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    r2_key: str | None
    status: str
    rejection_reason: str | None
    duration: float | None
    width: int | None
    height: int | None
    fps: float | None
    bitrate: int | None
    twelvelabs_video_id: str | None
    summary: str | None
    setting: str | None
    lighting_tags: list | None
    vibe_tags: list | None
    time_of_day: str | None
    color_temp_k: float | None
    avg_luminance: float | None
    dominant_palette: list | None
    camera_movement: str | None
    has_speech: bool | None
    has_music: bool | None
    quality_flags: list | None
    created_at: datetime
    indexed_at: datetime | None


class ClipDetail(ClipOut):
    """Full index record: clip fields + its segments + the Marengo embedding."""
    embedding: list | None = None
    segments: list[SegmentOut] = []


class ClipListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    summary: str | None
    width: int | None
    height: int | None
    fps: float | None
    duration: float | None
    created_at: datetime
    indexed_at: datetime | None


class ClipCreated(BaseModel):
    id: uuid.UUID
    status: str
