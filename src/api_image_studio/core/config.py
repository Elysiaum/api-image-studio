# -*- coding: utf-8 -*-
"""配置读写模块。

配置保存在「数据目录」的 config.json 中，包含 API 接入信息与默认生成参数。
API Key 只保存在本机，不会上传到任何第三方。

数据目录的确定规则：
  · 源码方式运行（开发）  -> 项目根目录
  · 打包成 exe 后运行      -> %APPDATA%\\API生图平台
    （安装目录通常在 Program Files 下不可写，必须放到用户目录）
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Dict

APP_DIR_NAME = "API生图平台"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的 exe 中。"""
    return bool(getattr(sys, "frozen", False))


def _find_project_root(start: str) -> str:
    """从 start 向上找到项目根目录（含 pyproject.toml / .git / requirements.txt）。"""
    cur = os.path.abspath(start)
    for _ in range(8):
        for marker in ("pyproject.toml", ".git", "requirements.txt"):
            if os.path.exists(os.path.join(cur, marker)):
                return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.abspath(start)


def _data_dir() -> str:
    if is_frozen():
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(root, APP_DIR_NAME)
    # 源码运行时，数据放在项目根目录（本文件位于 src/<包名>/core/）
    return _find_project_root(os.path.dirname(os.path.abspath(__file__)))


def _default_output_dir() -> str:
    """默认的图片保存位置：放在「图片」目录下，显眼好找。"""
    if is_frozen():
        home = os.path.expanduser("~")
        pics = os.path.join(home, "Pictures")
        base = pics if os.path.isdir(pics) else home
        return os.path.join(base, APP_DIR_NAME)
    return os.path.join(ROOT_DIR, "output")


# 可写数据目录
ROOT_DIR = _data_dir()
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")
HISTORY_PATH = os.path.join(ROOT_DIR, "history.json")
THUMB_DIR = os.path.join(ROOT_DIR, "thumbs")

# 默认图片输出目录（可在设置里改）
DEFAULT_OUTPUT_DIR = _default_output_dir()
OUTPUT_DIR = DEFAULT_OUTPUT_DIR  # 兼容旧引用

_lock = threading.Lock()

DEFAULT_CONFIG: Dict[str, Any] = {
    # ---- API 接入 ----
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-image-1",
    "models": [
        "gpt-image-1",
        "dall-e-3",
        "dall-e-2",
        "flux.1-schnell",
        "Kwai-Kolors/Kolors",
        "cogview-4",
        "wanx-v1",
    ],
    "timeout": 300,
    "proxy": "",            # 例如 http://127.0.0.1:7890 ，留空则使用系统代理
    "verify_ssl": True,
    "extra_body": "",       # 附加到请求体中的 JSON 字符串，用于兼容特殊中转站

    # ---- ComfyUI ----
    "engine": "api",        # api = OpenAI 兼容中转站；comfyui = 本地/远程 ComfyUI
    "comfy_url": "http://127.0.0.1:8188",
    "comfy_timeout": 600,
    "comfy_workflow": "",   # 工作流 JSON（API 格式）的文件路径
    "comfy_mapping": {},    # 提示词/尺寸/种子等对应的节点与字段

    # ---- 默认生成参数 ----
    "size": "1024x1024",
    "n": 1,
    "quality": "",          # 留空 = 不发送该字段
    "style": "",            # 留空 = 不发送该字段
    "seed": "",             # 留空 = 不发送该字段
    "negative_prompt": "",
    "response_format": "b64_json",

    # ---- 界面 ----
    "theme": "dark",
    "save_dir": "",         # 留空 = 使用 DEFAULT_OUTPUT_DIR
}


def output_dir(cfg: Dict[str, Any] | None = None) -> str:
    """解析当前生效的图片保存目录。"""
    cfg = cfg if cfg is not None else load_config()
    return (cfg.get("save_dir") or "").strip() or DEFAULT_OUTPUT_DIR


def ensure_dirs() -> None:
    for path in (ROOT_DIR, THUMB_DIR, output_dir()):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass


def load_config() -> Dict[str, Any]:
    """读取配置，缺失字段用默认值补齐。"""
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
        except (OSError, json.JSONDecodeError):
            # 配置损坏时回退到默认值，不阻断程序启动
            pass
    return cfg


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """把 patch 合并写入配置文件，返回最新完整配置。"""
    with _lock:
        cfg = load_config()
        for key, value in (patch or {}).items():
            cfg[key] = value
        try:
            os.makedirs(ROOT_DIR, exist_ok=True)
        except OSError:
            pass
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    return cfg


def mask_key(api_key: str) -> str:
    """把 Key 打码，仅用于回显。"""
    if not api_key:
        return ""
    if len(api_key) <= 10:
        return api_key[:2] + "***"
    return f"{api_key[:6]}...{api_key[-4:]}"
