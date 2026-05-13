from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT_DIR / "config.json"


class Config:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            self._data = json.load(f)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def path(self, key: str) -> Path:
        raw = self._data.get(key)
        if not raw:
            raise KeyError(f"Missing path config: {key}")
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT_DIR / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def videos_dir(self) -> Path:
        return self.path("videos_dir")

    @property
    def projects_dir(self) -> Path:
        return self.path("projects_dir")

    @property
    def output_dir(self) -> Path:
        return self.path("output_dir")

    @property
    def temp_dir(self) -> Path:
        return self.path("temp_dir")

    @property
    def assets_dir(self) -> Path:
        return self.path("assets_dir")

    @property
    def ffmpeg(self) -> str:
        return self._data.get("ffmpeg_path", "ffmpeg")

    @property
    def ffprobe(self) -> str:
        return self._data.get("ffprobe_path", "ffprobe")

    @property
    def encoder(self) -> str:
        return self._data.get("encoder", "h264_nvenc")

    @property
    def preset(self) -> str:
        return self._data.get("preset", "p6")

    @property
    def cq(self) -> int:
        return int(self._data.get("cq", 21))

    @property
    def use_hwaccel(self) -> bool:
        return bool(self._data.get("use_hwaccel", True))

    @property
    def intro_duration_seconds(self) -> float:
        return float(self._data.get("intro_duration_seconds", 6.0))

    @property
    def intro_avatar_size_px(self) -> int:
        return int(self._data.get("intro_avatar_size_px", 420))

    @property
    def intro_blur_sigma(self) -> int:
        return int(self._data.get("intro_blur_sigma", 30))

    # ------------------------------------------------------------------
    # Intermission card — typography bridge between highlight reel and
    # main match. When enabled, replaces the 0.8 s gold-sweep transition
    # (see backend/ass/transition.py) and suppresses the FULL MATCH
    # top-left badge in the main render so the headline doesn't read as
    # duplicated.
    # ------------------------------------------------------------------

    @property
    def intermission_enabled(self) -> bool:
        return bool(self._data.get("intermission_enabled", True))

    @property
    def intermission_text(self) -> str:
        return (self._data.get("intermission_text") or "FULL MATCH").strip() or "FULL MATCH"

    @property
    def intermission_duration_seconds(self) -> float:
        return float(self._data.get("intermission_duration_seconds", 3.0))

    def _optional_asset(self, key: str) -> Path | None:
        """Resolve a config-supplied asset path, returning None when the
        value is missing OR the file doesn't exist. Used by the
        intermission renderer to gracefully fall back to a solid-colour
        background / silent audio when the operator hasn't dropped real
        assets in yet."""
        raw = self._data.get(key)
        if not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT_DIR / p
        return p if p.exists() and p.is_file() else None

    @property
    def intermission_bg_path(self) -> Path | None:
        return self._optional_asset("intermission_bg_path")

    @property
    def intermission_sound_path(self) -> Path | None:
        return self._optional_asset("intermission_sound_path")

    # ------------------------------------------------------------------
    # Outro card — 5-second closing card appended after main.mp4. The
    # background is the last frame of main.mp4 blurred + dimmed; the
    # optional `outro_bg_path` is a fallback for when frame extraction
    # fails (e.g. main was disabled) or the operator wants a fixed bg.
    # ------------------------------------------------------------------

    @property
    def outro_enabled(self) -> bool:
        return bool(self._data.get("outro_enabled", True))

    @property
    def outro_text(self) -> str:
        return (self._data.get("outro_text") or "THANK YOU FOR WATCHING").strip() \
            or "THANK YOU FOR WATCHING"

    @property
    def outro_duration_seconds(self) -> float:
        return float(self._data.get("outro_duration_seconds", 5.0))

    @property
    def outro_bg_path(self) -> Path | None:
        return self._optional_asset("outro_bg_path")


config = Config()
