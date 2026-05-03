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


config = Config()
