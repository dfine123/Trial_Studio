"""Environment-driven settings (pydantic-settings).

All secrets/config come from the environment / `.env`. Nothing here is hardcoded.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Integrations ──────────────────────────────────────────
    anthropic_api_key: str = ""          # not used in Phase 0 (captions are Phase 1)
    twelvelabs_api_key: str = ""

    # ── Cloudflare R2 (S3-compatible) ─────────────────────────
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "trial-studio"
    r2_endpoint: str = ""

    # ── Infra ─────────────────────────────────────────────────
    database_url: str = ""
    redis_url: str = "redis://localhost:6379/0"

    # ── App knobs ─────────────────────────────────────────────
    twelvelabs_index_name: str = "trial-studio"
    tl_marengo_model: str = "marengo3.0"          # index embedding model (2.7 retired)
    tl_pegasus_model: str = "pegasus1.2"          # generative model for summary/tags
    enable_marengo_embedding: bool = True         # store the per-clip Marengo vector (extra cost)
    tl_min_duration: float = 4.0                  # TL's hard minimum; freeze-pad shorter clips
    tl_pad_target: float = 4.5                    # pad sub-minimum clips up to this length
    index_concurrency: int = 6                    # clips in flight (TL remote waits overlap; cv2 stays serialized)
    sync_max_clip_seconds: float = 20.0           # Drive sync skips clips longer than this (0 = no cap)
    clip_sim_threshold: float = 0.93              # within a reel, clips with embedding cosine >= this count as the SAME footage

    # ── Captions (Phase 1) ────────────────────────────────────
    caption_model: str = "claude-opus-4-8"        # Anthropic model for the Caption Assistant
    caption_provider: str = "anthropic"           # "anthropic" | "openai" — which LLM generates (per-instance via env)
    generation_engine: str = "v2"                 # "v2" = UNDERSTANDING-FIRST two-stage (ideate premises
                                                  # from persona+codex with catalog as TAKEN territory ->
                                                  # execute at the wall-as-bar; operator directive
                                                  # 2026-07-07: "orient for success, stop morphing the
                                                  # catalog"). "v1" = legacy anchor-rotation (rollback).
    reel_render_concurrency: int = 2              # batch generation: renders (clip-match + ffmpeg) that
                                                  # run in parallel. Captions stay SERIAL by design — the
                                                  # anti-repeat window and rotation state must see each
                                                  # slate before the next starts. ffmpeg is CPU-bound;
                                                  # raise only with more vCPU.
    chooser_model: str = "claude-sonnet-4-6"      # the SELECTION judge. Measured 2026-07-06 (frozen
                                                  # 22-case eval): opus-as-chooser picks the operator-
                                                  # REJECTED line 17/22 (taste inversion); sonnet-4-6 /
                                                  # sonnet-5 / haiku-4.5 all score 6/22 correct with 2
                                                  # loser-picks on the same prompt. Judge-model property,
                                                  # not prompt (5 prompt variants failed to move opus).
    coherence_gate: str = "off"                   # 'off' | 'log' | 'drop' — literal-read mechanism check on
                                                  # candidates. MEASURED NEGATIVE (round-3 replay, 2 prompt
                                                  # variants): recall 0/9 on known mechanism-kills at clean
                                                  # precision — the class is sloppy-mapping (taste), not parse
                                                  # failure; a judge can't split it from absurdism. Kept for
                                                  # future re-tests via /api/debug/gate-check.
    reskin_check: str = "drop"                    # 'drop' | 'log' | 'off' — IDENTITY-only screen for semantic
                                                  # re-skins (same joke wearing new nouns) that word-overlap
                                                  # guards can't see (the raccoons->hyenas class, 2026-07-10
                                                  # revitalization). Identity classification like the labeler,
                                                  # never a quality judge; fail-open on any error.
    openai_api_key: str = ""
    openai_caption_model: str = "gpt-4o"          # OpenAI model for the A/B (override via OPENAI_CAPTION_MODEL)

    # ── Google Drive ingest (service account; share a folder with the SA email) ──
    google_sa_json: str = ""           # raw service-account key JSON (use on Railway — paste contents)
    google_sa_json_file: str = ""      # OR a path to the key file (use locally — no JSON in .env)

    # ── Google Drive EXPORT (OAuth as the operator — SAs can't own files in a personal My Drive) ──
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_refresh_token: str = ""
    drive_export_root: str = "treelz exports"     # top-level folder in the operator's My Drive

    @property
    def google_sa_info(self) -> dict | None:
        """The service-account key as a dict, from GOOGLE_SA_JSON (contents) or GOOGLE_SA_JSON_FILE
        (path) — whichever is set. None if Drive ingest isn't configured."""
        import json
        raw = (self.google_sa_json or "").strip()
        if not raw and self.google_sa_json_file:
            try:
                with open(self.google_sa_json_file, encoding="utf-8") as f:
                    raw = f.read()
            except OSError:
                return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    @property
    def google_sa_email(self) -> str | None:
        info = self.google_sa_info
        return info.get("client_email") if info else None

    # ── Reel assembly (Phase 1) ───────────────────────────────
    reel_target_shot: float = 2.0    # ~seconds per shot; each cut snaps to the nearest beat
    reel_min_shot: float = 1.0        # don't leave a final shard shorter than this
    reel_max_shot: float = 3.2       # split any longer slot — a sparse/empty beat map must never
                                     # produce one giant slot no clip can fill (that froze a reel)
    font_path: str = "fonts/TikTokSans-VariableFont.ttf"   # caption brand font
    reel_width: int = 1080
    reel_height: int = 1920
    reel_fps: int = 30
    work_dir: str = "/tmp/trial-studio"          # transient per-clip working space
    min_resolution: int = 720                    # QC: reject if min(w,h) < this (720p phone/download footage is fine)
    min_fps: float = 23.0                         # QC: reject if fps < this (accept 24fps content)

    # ── Validated-reel export (Google Drive for Desktop sync) ─
    # Defaults to a folder on the persistent volume (var/validated); set REEL_EXPORT_DIR to a
    # Drive-synced folder later for auto-upload. Portable across local + Railway.
    reel_export_dir: str = "var/validated"

    # ── treelz.ai front-end (local demo auth) ─────────────────
    treelz_user: str = "dfine"
    treelz_password: str = "cool123"
    treelz_secret: str = "treelz-local-dev-secret"   # signs the session cookie; override via env in prod

    # ── DEMO MODE (the friends-demo deployment: same repo, second Railway service) ──
    # DEMO_MODE=1 flips the service into the public demo: open signup (each account = its own
    # profile generating with the seeded Base voice), a locked-down route whitelist (no operator
    # surfaces, no debug endpoints), per-user caps below. Prod runs with this OFF — everything
    # demo is dormant.
    demo_mode: bool = False
    demo_max_clips: int = 50              # per-user clip library cap (upload rejected past this)
    demo_max_clip_seconds: float = 30.0   # per-clip duration cap (QC-rejected past this)
    demo_reels_per_window: int = 15       # reels per user per window...
    demo_cooldown_hours: float = 24.0     # ...then this cooldown, then the counter fully resets

    @property
    def sqlalchemy_url(self) -> str:
        """Normalize a bare Postgres URL (e.g. Railway's) to the psycopg driver."""
        url = self.database_url
        if url.startswith("postgresql+"):
            return url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+psycopg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
