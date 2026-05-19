"""Project save / load / delete routes."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from ..config import config
from ..models import ProjectData
from .utils import _resolve_inside, _safe_name


router = APIRouter()


@router.get("/api/projects")
def list_projects() -> dict:
    items = []
    for p in sorted(config.projects_dir.iterdir()):
        if p.is_file() and p.suffix.lower() == ".json":
            items.append({"name": p.stem, "modified": p.stat().st_mtime})
    return {"projects": items}


@router.get("/api/projects/{name}")
def get_project(name: str) -> dict:
    name = _safe_name(name)
    target = _resolve_inside(config.projects_dir, f"{name}.json")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    with open(target, "r", encoding="utf-8") as f:
        return json.load(f)


@router.put("/api/projects/{name}")
def save_project(name: str, project: ProjectData) -> dict:
    name = _safe_name(name)
    target = _resolve_inside(config.projects_dir, f"{name}.json")
    with open(target, "w", encoding="utf-8") as f:
        json.dump(project.model_dump(), f, ensure_ascii=False, indent=2)
    return {"ok": True, "name": name, "path": str(target)}


@router.delete("/api/projects/{name}")
def delete_project(name: str) -> dict:
    name = _safe_name(name)
    target = _resolve_inside(config.projects_dir, f"{name}.json")
    if target.exists():
        target.unlink()
    return {"ok": True}
