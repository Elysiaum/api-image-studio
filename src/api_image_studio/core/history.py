# -*- coding: utf-8 -*-
"""历史记录模块。

每条记录保存提示词、参数与图片路径，图片文件本身落在 output 目录。
同时用 Pillow 生成缩略图，保证画廊滚动流畅。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List

from .config import HISTORY_PATH, ROOT_DIR, THUMB_DIR, load_config, output_dir

_lock = threading.Lock()
THUMB_MAX = 480


def _ensure_thumb_dir() -> None:
    os.makedirs(THUMB_DIR, exist_ok=True)


def load_history() -> List[Dict[str, Any]]:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_history(records: List[Dict[str, Any]]) -> None:
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HISTORY_PATH)


def make_thumbnail(image_path: str) -> str:
    """生成缩略图，失败则返回原图路径。"""
    try:
        from PIL import Image
    except ImportError:
        return image_path
    try:
        _ensure_thumb_dir()
        name = os.path.splitext(os.path.basename(image_path))[0] + ".jpg"
        thumb_path = os.path.join(THUMB_DIR, name)
        if os.path.exists(thumb_path) and os.path.getmtime(thumb_path) >= os.path.getmtime(image_path):
            return thumb_path
        with Image.open(image_path) as im:
            im = im.convert("RGB")
            im.thumbnail((THUMB_MAX, THUMB_MAX))
            im.save(thumb_path, "JPEG", quality=86)
        return thumb_path
    except Exception:
        return image_path


def add_record(
    *,
    prompt: str,
    negative_prompt: str,
    params: Dict[str, Any],
    images: List[Dict[str, Any]],
    mode: str,
    elapsed: float,
    warnings: List[str] | None = None,
) -> Dict[str, Any]:
    record = {
        "id": uuid.uuid4().hex[:12],
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": time.time(),
        "mode": mode,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "params": params,
        "elapsed": elapsed,
        "warnings": warnings or [],
        "images": [
            {
                "path": img["path"],
                "filename": img["filename"],
                "thumb": make_thumbnail(img["path"]),
                "url": img.get("url", ""),
                "revised_prompt": img.get("revised_prompt", ""),
                "size_bytes": img.get("size_bytes", 0),
            }
            for img in images
        ],
    }
    with _lock:
        records = load_history()
        records.insert(0, record)
        # 只保留最近 500 条，避免历史文件无限膨胀
        if len(records) > 500:
            records = records[:500]
        _write_history(records)
    return record


def delete_record(record_id: str) -> bool:
    with _lock:
        records = load_history()
        kept = [r for r in records if r.get("id") != record_id]
        if len(kept) == len(records):
            return False
        _write_history(kept)
    return True


def clear_history() -> None:
    with _lock:
        _write_history([])


def stats() -> Dict[str, Any]:
    records = load_history()
    count = sum(len(r.get("images", [])) for r in records)
    return {"records": len(records), "images": count}


def to_rel(path: str) -> str:
    """转成相对于项目根目录的路径，便于前端通过本地服务访问。"""
    try:
        rel = os.path.relpath(path, ROOT_DIR)
    except ValueError:
        return path
    return rel.replace("\\", "/")


def current_output_dir() -> str:
    return output_dir()
