# -*- coding: utf-8 -*-
"""API 生图平台 —— 本地桌面生图工具。

支持两种生图后端：
  · OpenAI 兼容 API 中转站（文生图 / 图生图）
  · 本地或远程 ComfyUI（导入 API 格式工作流）

所有请求由本机直接发往你配置的接口，不经过任何中间服务器。
"""
from __future__ import annotations

__version__ = "1.0.0"
__app_name__ = "API 生图平台"
__app_name_ascii__ = "API Image Studio"
__author__ = "Elysiaum"
__license__ = "MIT"

__all__ = ["__version__", "__app_name__", "__app_name_ascii__"]
