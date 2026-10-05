# -*- coding: utf-8 -*-
"""图形界面（CustomTkinter）。

不依赖 WebView2，在 Windows 11 上稳定可靠。所有生图请求由本机直接发往
用户配置的接口，不经过任何中间服务器。

功能：
  · 文生图 / 图生图（垫图）
  · 批量生成、完整参数面板（尺寸 / 数量 / 质量 / 风格 / 种子 / 返回格式）
  · ComfyUI 后端（导入 API 格式工作流，自动识别节点映射，WebSocket 实时进度）
  · 历史记录（回看、复用提示词、删除）
  · 自由接入任意 OpenAI 兼容 API 中转站
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import customtkinter as ctk
from PIL import Image
from tkinter import filedialog, messagebox

from . import __app_name__, __version__
from .core import config as cfg_mod
from .core import history as hist_mod
from .core.api_client import ImageAPIClient
from .core.comfy_client import ComfyUIClient

APP_NAME = __app_name__
UI_FONT = "Microsoft YaHei UI"
THUMB_SIZE = 286
PREVIEW_MAX = (980, 720)

SIZE_OPTIONS = [
    "1024x1024", "1024x1536", "1536x1024", "512x512", "768x768",
    "768x1024", "1024x768", "1280x720", "720x1280", "自定义…",
]
QUALITY_OPTIONS = ["默认", "standard", "hd", "high", "medium", "low"]
STYLE_OPTIONS = ["默认", "vivid", "natural"]
FORMAT_OPTIONS = ["b64_json", "不指定", "url"]


def asset_path(name: str) -> str:
    """定位随程序分发的资源文件（打包后与源码运行都能找到）。"""
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "assets", name))
        candidates.append(os.path.join(base, name))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "assets", name))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ""


def font(size: int = 13, weight: str = "normal"):
    return ctk.CTkFont(family=UI_FONT, size=size, weight=weight)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.cfg: Dict[str, Any] = cfg_mod.load_config()
        ctk.set_appearance_mode(self.cfg.get("theme", "dark"))
        ctk.set_default_color_theme("dark-blue")

        self.title(f"{APP_NAME} {__version__}")
        self.geometry("1440x930")
        self.minsize(1120, 720)
        icon = asset_path("app.ico")
        if icon:
            try:
                self.iconbitmap(icon)
            except Exception:  # noqa: BLE001
                pass

        self.ref_image: Optional[str] = None
        self.generating = False
        self.records: List[Dict[str, Any]] = []
        self.last_images: List[Dict[str, Any]] = []
        self._img_refs: Dict[str, Any] = {}      # 防止缩略图被回收
        self._model_tags: List[str] = list(self.cfg.get("models") or [])
        self._page = "create"

        # ComfyUI 相关状态
        self.engine: str = self.cfg.get("engine") or "api"
        self._workflow: Dict[str, Any] = {}
        self._workflow_path: str = self.cfg.get("comfy_workflow") or ""
        self._analysis: Dict[str, Any] = {}
        self._map_choices: Dict[str, Dict[str, Any]] = {}
        self._comfy_mapping: Dict[str, Any] = dict(self.cfg.get("comfy_mapping") or {})

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_topbar()
        self._build_statusbar()

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 6))
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

        self.pages: Dict[str, ctk.CTkFrame] = {}
        self._build_create_page()
        self._build_history_page()
        self._build_settings_page()
        self._apply_config_to_form()

        self._show_page("create")
        self._refresh_model_tags()
        self._refresh_status_chip()
        self.load_history()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Control-Return>", lambda e: self.on_generate())

        # 后台线程 -> 界面的唯一通道（tkinter 只能在主线程里操作控件）
        self._ui_queue: "queue.Queue" = queue.Queue()
        self.after(50, self._drain_ui_queue)

    def _drain_ui_queue(self) -> None:
        """在主线程里消费后台线程投递过来的界面更新。"""
        try:
            while True:
                func, args = self._ui_queue.get_nowait()
                try:
                    func(*args)
                except Exception:  # noqa: BLE001
                    pass
        except queue.Empty:
            pass
        try:
            self.after(50, self._drain_ui_queue)
        except Exception:  # noqa: BLE001
            pass

    def _post(self, func, *args) -> None:
        """从任意线程安全地请求更新界面。"""
        self._ui_queue.put((func, args))

    # ================= 顶栏 / 状态栏 =================

    def _build_topbar(self) -> None:
        bar = ctk.CTkFrame(self, height=58, corner_radius=0)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.pack(side="left", padx=16)
        ctk.CTkLabel(left, text="◆", font=font(20, "bold"),
                     text_color=("#3B82F6", "#7C6CFF")).pack(side="left", padx=(0, 8))
        titles = ctk.CTkFrame(left, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(titles, text=APP_NAME, font=font(15, "bold"),
                     anchor="w").pack(anchor="w")
        self.lbl_sub = ctk.CTkLabel(titles, text="未配置", font=font(11),
                                    text_color=("gray45", "gray60"), anchor="w")
        self.lbl_sub.pack(anchor="w")

        self.tab_buttons: Dict[str, ctk.CTkButton] = {}
        mid = ctk.CTkFrame(bar, fg_color="transparent")
        mid.pack(side="left", padx=26)
        for key, text in (("create", "生成"), ("history", "历史"), ("settings", "设置")):
            b = ctk.CTkButton(
                mid, text=text, width=92, height=34, corner_radius=9,
                font=font(13, "bold"), fg_color="transparent",
                text_color=("gray20", "gray80"), hover_color=("gray85", "gray25"),
                command=lambda k=key: self._show_page(k),
            )
            b.pack(side="left", padx=3)
            self.tab_buttons[key] = b

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.pack(side="right", padx=16)
        ctk.CTkButton(right, text="输出文件夹", width=96, height=32, corner_radius=8,
                      font=font(12), fg_color="transparent", border_width=1,
                      text_color=("gray20", "gray80"),
                      command=self._open_output_dir).pack(side="right", padx=4)
        self.btn_theme = ctk.CTkButton(
            right, text=self._theme_icon(), width=38, height=32, corner_radius=8,
            font=font(14), fg_color="transparent", border_width=1,
            command=self._toggle_theme,
        )
        self.btn_theme.pack(side="right", padx=4)

    def _build_statusbar(self) -> None:
        self.status = ctk.CTkLabel(self, text="就绪", font=font(12),
                                   text_color=("gray40", "gray60"), anchor="w")
        self.status.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 8))

    def _status(self, text: str, kind: str = "info") -> None:
        colors = {
            "info": ("gray40", "gray60"),
            "ok": ("#15803D", "#4ADE80"),
            "err": ("#B91C1C", "#F87171"),
            "warn": ("#B45309", "#FBBF24"),
        }
        self.status.configure(text=text, text_color=colors.get(kind, colors["info"]))

    def _theme_icon(self) -> str:
        return "☾" if ctk.get_appearance_mode() == "Dark" else "☀"

    def _toggle_theme(self) -> None:
        nxt = "light" if ctk.get_appearance_mode() == "Dark" else "dark"
        ctk.set_appearance_mode(nxt)
        self.btn_theme.configure(text=self._theme_icon())
        self.cfg = cfg_mod.save_config({"theme": nxt})
        if hasattr(self, "opt_theme"):
            self.opt_theme.set("深色" if nxt == "dark" else "浅色")

    # ================= 页面切换 =================

    def _show_page(self, key: str) -> None:
        self._page = key
        for k, page in self.pages.items():
            page.grid_forget() if k != key else page.grid(row=0, column=0, sticky="nsew")
        for k, b in self.tab_buttons.items():
            if k == key:
                b.configure(fg_color=("#DBEAFE", "#2B3245"),
                            text_color=("#1D4ED8", "#C7CDFF"))
            else:
                b.configure(fg_color="transparent",
                            text_color=("gray20", "gray80"))
        if key == "history":
            self.load_history()

    # ================= 生成页 =================

    def _build_create_page(self) -> None:
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        page.grid_columnconfigure(1, weight=1)
        page.grid_rowconfigure(0, weight=1)
        self.pages["create"] = page

        # ---- 左侧参数面板 ----
        left = ctk.CTkScrollableFrame(page, width=412, corner_radius=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_columnconfigure(0, weight=1)

        r = 0

        def section(title: str) -> None:
            nonlocal r
            ctk.CTkLabel(left, text=title, font=font(12, "bold"),
                         text_color=("gray35", "gray65"), anchor="w"
                         ).grid(row=r, column=0, sticky="ew", pady=(14, 6))
            r += 1

        def hint(text: str) -> None:
            nonlocal r
            ctk.CTkLabel(left, text=text, font=font(11), anchor="w", justify="left",
                         wraplength=372, text_color=("gray50", "gray65")
                         ).grid(row=r, column=0, sticky="ew", pady=(0, 4))
            r += 1

        def block(widget) -> None:
            nonlocal r
            widget.grid(row=r, column=0, sticky="ew", pady=(0, 4))
            r += 1

        # ---- 生成引擎 ----
        section("生成引擎")
        self.seg_engine = ctk.CTkSegmentedButton(
            left, values=["API 中转站", "ComfyUI"], height=34, font=font(12),
            command=self._on_engine_change)
        self.seg_engine.set("API 中转站")
        block(self.seg_engine)

        # ---- 提示词 ----
        section("提示词")
        hint("描述你想要的画面，越具体越好；按 Ctrl+Enter 可快速生成")
        self.txt_prompt = ctk.CTkTextbox(left, height=112, corner_radius=9,
                                         font=font(13), wrap="word")
        block(self.txt_prompt)

        # ---- 负向提示词 ----
        self.chk_neg = ctk.CTkCheckBox(left, text="展开负向提示词", font=font(12),
                                       checkbox_width=17, checkbox_height=17,
                                       command=self._toggle_negative)
        block(self.chk_neg)
        self.txt_negative = ctk.CTkTextbox(left, height=62, corner_radius=9,
                                           font=font(12), wrap="word")
        self.txt_negative.grid(row=r, column=0, sticky="ew", pady=(0, 4))
        self.txt_negative.grid_remove()
        r += 1

        # ---- 参考图 ----
        section("参考图（图生图 / 垫图）")
        ref_box = ctk.CTkFrame(left, corner_radius=10, height=88)
        block(ref_box)
        ref_box.grid_propagate(False)
        self.lbl_ref = ctk.CTkLabel(ref_box, text="未选择参考图\n不选即为「文生图」",
                                    font=font(12), text_color=("gray45", "gray60"),
                                    justify="center")
        self.lbl_ref.pack(side="left", expand=True, fill="both")
        self.lbl_ref_img = ctk.CTkLabel(ref_box, text="")
        self.lbl_ref_img.pack(side="left", padx=10)
        btns = ctk.CTkFrame(ref_box, fg_color="transparent")
        btns.pack(side="right", padx=10)
        ctk.CTkButton(btns, text="选择", width=62, height=30, corner_radius=8, font=font(12),
                      command=self._pick_reference).pack(pady=(0, 6))
        ctk.CTkButton(btns, text="移除", width=62, height=30, corner_radius=8, font=font(12),
                      fg_color="transparent", border_width=1,
                      command=self._clear_reference).pack()

        # ---- 通用参数（两种引擎都用）----
        section("参数")
        g1 = ctk.CTkFrame(left, fg_color="transparent")
        block(g1)
        g1.grid_columnconfigure((0, 1, 2), weight=1)

        def cell(parent, row, col, label, factory):
            """「标题 + 控件」的小格。"""
            f = ctk.CTkFrame(parent, fg_color="transparent")
            f.grid(row=row, column=col, sticky="ew", padx=3, pady=4)
            ctk.CTkLabel(f, text=label, font=font(11), anchor="w",
                         text_color=("gray45", "gray60")).pack(anchor="w", fill="x")
            widget = factory(f)
            widget.pack(fill="x", pady=(2, 0))
            return widget

        self.opt_size = cell(g1, 0, 0, "尺寸", lambda p: ctk.CTkOptionMenu(
            p, values=SIZE_OPTIONS, height=32, font=font(12), corner_radius=8,
            command=self._on_size_change))
        self.ent_n = cell(g1, 0, 1, "数量", lambda p: ctk.CTkEntry(
            p, height=32, corner_radius=8, font=font(12)))
        self.ent_seed = cell(g1, 0, 2, "种子", lambda p: ctk.CTkEntry(
            p, height=32, corner_radius=8, font=font(12), placeholder_text="随机"))

        self.ent_custom_size = ctk.CTkEntry(left, height=32, corner_radius=8, font=font(12),
                                            placeholder_text="自定义尺寸，例如 1024x1024")
        self.ent_custom_size.grid(row=r, column=0, sticky="ew", pady=(2, 4))
        self.ent_custom_size.grid_remove()
        r += 1

        # ---- 引擎相关区域 ----
        self.engine_area = ctk.CTkFrame(left, fg_color="transparent")
        block(self.engine_area)
        self.engine_area.grid_columnconfigure(0, weight=1)
        self._build_api_section(self.engine_area)
        self._build_comfy_section(self.engine_area)

        # ---- 状态 ----
        self.chip = ctk.CTkFrame(left, corner_radius=9, height=38)
        block(self.chip)
        self.chip.grid_propagate(False)
        self.lbl_chip_dot = ctk.CTkLabel(self.chip, text="●", font=font(13),
                                         text_color=("#9CA3AF", "#6B7280"))
        self.lbl_chip_dot.pack(side="left", padx=(12, 6))
        self.lbl_chip = ctk.CTkLabel(self.chip, text="未配置 API", font=font(12),
                                     text_color=("gray35", "gray65"), anchor="w")
        self.lbl_chip.pack(side="left", expand=True)

        ctk.CTkLabel(left, text="", height=6).grid(row=r, column=0)
        r += 1

        # 生成按钮（固定在面板底部，不随滚动）
        footer = ctk.CTkFrame(page, fg_color="transparent", width=392)
        footer.grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=(10, 0))
        self.btn_generate = ctk.CTkButton(
            footer, text="开始生成", height=46, corner_radius=10,
            font=font(15, "bold"), command=self.on_generate,
        )
        self.btn_generate.pack(fill="x")

        # ---- 右侧结果区 ----
        right = ctk.CTkFrame(page, corner_radius=12)
        right.grid(row=0, column=1, rowspan=2, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(right, fg_color="transparent", height=48)
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 0))
        ctk.CTkLabel(head, text="生成结果", font=font(15, "bold")).pack(side="left")
        self.lbl_result_meta = ctk.CTkLabel(head, text="", font=font(12),
                                            text_color=("gray45", "gray60"))
        self.lbl_result_meta.pack(side="left", padx=12)
        ctk.CTkButton(head, text="清空", width=64, height=30, corner_radius=8, font=font(12),
                      fg_color="transparent", border_width=1,
                      command=self._clear_results).pack(side="right")
        self.btn_export = ctk.CTkButton(head, text="全部另存到桌面", width=118, height=30,
                                        corner_radius=8, font=font(12), fg_color="transparent",
                                        border_width=1, command=self._export_all)

        self.gallery = ctk.CTkScrollableFrame(right, corner_radius=0, fg_color="transparent")
        self.gallery.grid(row=1, column=0, sticky="nsew", padx=8, pady=(4, 8))
        for c in range(3):
            self.gallery.grid_columnconfigure(c, weight=1, uniform="gal")

        self.lbl_empty = ctk.CTkLabel(
            self.gallery, text="还没有生成结果\n\n在左侧填写提示词，然后点击「开始生成」",
            font=font(13), text_color=("gray55", "gray50"), justify="center")
        self.lbl_empty.grid(row=0, column=0, columnspan=3, pady=90)

        prog = ctk.CTkFrame(right, fg_color="transparent")
        prog.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 12))
        self.lbl_progress = ctk.CTkLabel(prog, text="", font=font(12), anchor="w",
                                         text_color=("gray40", "gray60"))
        self.progress = ctk.CTkProgressBar(prog, height=6, corner_radius=3)
        self.progress.set(0)

    # ---- API 中转站区块 ----
    def _build_api_section(self, parent) -> None:
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.grid(row=0, column=0, sticky="ew")
        f.grid_columnconfigure(0, weight=1)
        self.frame_api = f

        def title(text: str, row: int) -> None:
            ctk.CTkLabel(f, text=text, font=font(12, "bold"), anchor="w",
                         text_color=("gray35", "gray65")).grid(
                row=row, column=0, sticky="ew", pady=(14, 6))

        def label(text: str, row: int) -> None:
            ctk.CTkLabel(f, text=text, font=font(11), anchor="w",
                         text_color=("gray45", "gray60")).grid(
                row=row, column=0, sticky="ew", pady=(6, 0))

        title("API 中转站", 0)
        label("模型", 1)
        self.ent_model = ctk.CTkEntry(f, height=36, corner_radius=9, font=font(13),
                                      placeholder_text="例如 gpt-image-1")
        self.ent_model.grid(row=2, column=0, sticky="ew", pady=(2, 4))
        self.ent_model.bind("<KeyRelease>", lambda e: self._refresh_status_chip())
        self.opt_model = ctk.CTkOptionMenu(f, height=30, font=font(12), corner_radius=8,
                                           values=["（常用模型）"], command=self._pick_model)
        self.opt_model.grid(row=3, column=0, sticky="ew")

        g = ctk.CTkFrame(f, fg_color="transparent")
        g.grid(row=4, column=0, sticky="ew")
        g.grid_columnconfigure((0, 1), weight=1)

        def cell2(col, text, factory):
            box = ctk.CTkFrame(g, fg_color="transparent")
            box.grid(row=0, column=col, sticky="ew", padx=3, pady=4)
            ctk.CTkLabel(box, text=text, font=font(11), anchor="w",
                         text_color=("gray45", "gray60")).pack(anchor="w", fill="x")
            w = factory(box)
            w.pack(fill="x", pady=(2, 0))
            return w

        self.opt_quality = cell2(0, "质量", lambda p: ctk.CTkOptionMenu(
            p, values=QUALITY_OPTIONS, height=32, font=font(12), corner_radius=8))
        self.opt_style = cell2(1, "风格", lambda p: ctk.CTkOptionMenu(
            p, values=STYLE_OPTIONS, height=32, font=font(12), corner_radius=8))

        self.chk_adv = ctk.CTkCheckBox(f, text="高级选项", font=font(12),
                                       checkbox_width=17, checkbox_height=17,
                                       command=self._toggle_advanced)
        self.chk_adv.grid(row=5, column=0, sticky="w", pady=(12, 0))

        self.adv_frame = ctk.CTkFrame(f, fg_color="transparent")
        self.adv_frame.grid(row=6, column=0, sticky="ew")
        self.adv_frame.grid_columnconfigure(0, weight=1)
        self.adv_frame.grid_remove()

        ctk.CTkLabel(self.adv_frame, text="图生图调用方式", font=font(11),
                     text_color=("gray45", "gray60"), anchor="w").grid(row=0, column=0, sticky="ew")
        self.opt_img2img = ctk.CTkOptionMenu(
            self.adv_frame, height=32, font=font(12), corner_radius=8,
            values=["/images/edits（标准，推荐）", "/images/generations + image 字段"])
        self.opt_img2img.grid(row=1, column=0, sticky="ew", pady=(2, 8))

        ctk.CTkLabel(self.adv_frame, text="返回格式", font=font(11),
                     text_color=("gray45", "gray60"), anchor="w").grid(row=2, column=0, sticky="ew")
        self.opt_format = ctk.CTkOptionMenu(self.adv_frame, values=FORMAT_OPTIONS, height=32,
                                            font=font(12), corner_radius=8)
        self.opt_format.grid(row=3, column=0, sticky="ew", pady=(2, 8))

        ctk.CTkLabel(self.adv_frame, text="附加请求体 JSON（特殊中转站用）", font=font(11),
                     text_color=("gray45", "gray60"), anchor="w").grid(row=4, column=0, sticky="ew")
        self.txt_extra = ctk.CTkTextbox(self.adv_frame, height=58, corner_radius=8, font=font(12))
        self.txt_extra.grid(row=5, column=0, sticky="ew", pady=(2, 0))

    # ---- ComfyUI 区块 ----
    def _build_comfy_section(self, parent) -> None:
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.grid(row=0, column=0, sticky="ew")
        f.grid_columnconfigure(0, weight=1)
        f.grid_remove()
        self.frame_comfy = f

        ctk.CTkLabel(f, text="ComfyUI 服务", font=font(12, "bold"), anchor="w",
                     text_color=("gray35", "gray65")).grid(row=0, column=0, sticky="ew", pady=(14, 6))

        row = ctk.CTkFrame(f, fg_color="transparent")
        row.grid(row=1, column=0, sticky="ew")
        row.grid_columnconfigure(0, weight=1)
        self.ent_comfy_url = ctk.CTkEntry(row, height=34, corner_radius=9, font=font(12),
                                          placeholder_text="http://127.0.0.1:8188")
        self.ent_comfy_url.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(row, text="测试", width=58, height=34, corner_radius=9, font=font(12),
                      command=self._test_comfy).grid(row=0, column=1, padx=(8, 0))

        self.lbl_comfy_state = ctk.CTkLabel(f, text="", font=font(11), anchor="w",
                                            wraplength=372, justify="left",
                                            text_color=("gray50", "gray65"))
        self.lbl_comfy_state.grid(row=2, column=0, sticky="ew", pady=(4, 0))

        # 工作流
        ctk.CTkLabel(f, text="工作流（ComfyUI 里用「导出（API格式）」得到）",
                     font=font(11), anchor="w", text_color=("gray45", "gray60")
                     ).grid(row=3, column=0, sticky="ew", pady=(12, 4))
        wrow = ctk.CTkFrame(f, fg_color="transparent")
        wrow.grid(row=4, column=0, sticky="ew")
        wrow.grid_columnconfigure(0, weight=1)
        self.lbl_workflow = ctk.CTkLabel(
            wrow, text="尚未导入工作流", font=font(11), anchor="w",
            text_color=("gray50", "gray65"), wraplength=250, justify="left")
        self.lbl_workflow.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(wrow, text="导入 JSON", width=84, height=32, corner_radius=8,
                      font=font(12), command=self._import_workflow).grid(row=0, column=1, padx=(8, 0))

        # 节点映射
        ctk.CTkLabel(f, text="节点映射（自动识别，可手动改）", font=font(11), anchor="w",
                     text_color=("gray45", "gray60")).grid(row=5, column=0, sticky="ew", pady=(14, 4))

        self.map_menus: Dict[str, Any] = {}
        self._map_rows = [
            ("positive", "正向提示词"),
            ("negative", "负向提示词"),
            ("size", "尺寸"),
            ("seed", "种子"),
            ("batch", "批量张数"),
            ("image", "参考图"),
        ]
        for i, (key, text) in enumerate(self._map_rows):
            ctk.CTkLabel(f, text=text, font=font(11), anchor="w",
                         text_color=("gray45", "gray60")).grid(
                row=6 + i * 2, column=0, sticky="ew", pady=(6, 0))
            menu = ctk.CTkOptionMenu(
                f, height=32, font=font(11), corner_radius=8, values=["（请先导入工作流）"],
                command=lambda v, k=key: self._comfy_select(k, v))
            menu.grid(row=7 + i * 2, column=0, sticky="ew")
            menu.configure(state="disabled")
            self.map_menus[key] = menu

        # 超时
        trow = ctk.CTkFrame(f, fg_color="transparent")
        trow.grid(row=19, column=0, sticky="ew", pady=(14, 0))
        trow.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(trow, text="超时（秒）", font=font(11), anchor="w",
                     text_color=("gray45", "gray60")).grid(row=0, column=0, sticky="ew")
        self.ent_comfy_timeout = ctk.CTkEntry(trow, height=32, corner_radius=8, font=font(12))
        self.ent_comfy_timeout.grid(row=1, column=0, sticky="ew", pady=(2, 0))


    # ================= 历史页 =================

    def _build_history_page(self) -> None:
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        self.pages["history"] = page

        head = ctk.CTkFrame(page, fg_color="transparent", height=44)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ctk.CTkLabel(head, text="历史记录", font=font(15, "bold")).pack(side="left")
        self.lbl_hist_meta = ctk.CTkLabel(head, text="", font=font(12),
                                          text_color=("gray45", "gray60"))
        self.lbl_hist_meta.pack(side="left", padx=12)
        ctk.CTkButton(head, text="清空历史", width=90, height=30, corner_radius=8,
                      font=font(12), fg_color="transparent", border_width=1,
                      command=self._clear_history).pack(side="right")

        self.hist_scroll = ctk.CTkScrollableFrame(page, corner_radius=12)
        self.hist_scroll.grid(row=1, column=0, sticky="nsew")
        self.hist_scroll.grid_columnconfigure(0, weight=1)

    # ================= 设置页 =================

    def _build_settings_page(self) -> None:
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(0, weight=1)
        self.pages["settings"] = page

        wrap = ctk.CTkScrollableFrame(page, corner_radius=12)
        wrap.grid(row=0, column=0, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)

        r = 0

        def card(title: str, desc: str = "") -> ctk.CTkFrame:
            nonlocal r
            c = ctk.CTkFrame(wrap, corner_radius=11)
            c.grid(row=r, column=0, sticky="ew", pady=(0, 14), padx=4)
            c.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(c, text=title, font=font(14, "bold"), anchor="w"
                         ).grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 2))
            rr = 1
            if desc:
                ctk.CTkLabel(c, text=desc, font=font(11), anchor="w", justify="left",
                             text_color=("gray45", "gray60")
                             ).grid(row=rr, column=0, sticky="ew", padx=18, pady=(0, 8))
                rr += 1
            c._next = rr  # type: ignore[attr-defined]
            r += 1
            return c

        def field(c: ctk.CTkFrame, label: str, widget, hint: str = ""):
            rr = c._next  # type: ignore[attr-defined]
            ctk.CTkLabel(c, text=label, font=font(12, "bold"), anchor="w"
                         ).grid(row=rr, column=0, sticky="ew", padx=18, pady=(8, 3))
            widget.grid(row=rr + 1, column=0, sticky="ew", padx=18)
            c._next = rr + 2  # type: ignore[attr-defined]
            if hint:
                ctk.CTkLabel(c, text=hint, font=font(11), anchor="w", justify="left",
                             text_color=("gray50", "gray65")
                             ).grid(row=c._next, column=0, sticky="ew", padx=18, pady=(3, 0))  # type: ignore[attr-defined]
                c._next += 1  # type: ignore[attr-defined]

        # --- API 接入 ---
        c1 = card("API 接入", "填入中转站地址与密钥即可，支持任何 OpenAI 兼容的图像接口。")
        self.ent_base = ctk.CTkEntry(c1, height=36, corner_radius=9, font=font(12),
                                     placeholder_text="https://api.openai.com/v1")
        field(c1, "Base URL", self.ent_base,
              "填到 /v1 即可，程序会自动拼接 /images/generations；直接粘贴完整接口地址也行。")

        key_row = ctk.CTkFrame(c1, fg_color="transparent")
        key_row.grid_columnconfigure(0, weight=1)
        self.ent_key = ctk.CTkEntry(key_row, height=36, corner_radius=9, font=font(12),
                                    placeholder_text="sk-...", show="•")
        self.ent_key.grid(row=0, column=0, sticky="ew")
        self.btn_show_key = ctk.CTkButton(key_row, text="显示", width=56, height=36,
                                          corner_radius=9, font=font(12), fg_color="transparent",
                                          border_width=1, command=self._toggle_key)
        self.btn_show_key.grid(row=0, column=1, padx=(8, 0))
        field(c1, "API Key", key_row, "密钥只保存在本机 config.json，不会上传到任何第三方。")

        self.ent_default_model = ctk.CTkEntry(c1, height=36, corner_radius=9, font=font(12),
                                              placeholder_text="gpt-image-1")
        field(c1, "默认模型", self.ent_default_model)

        ctk.CTkLabel(c1, text="常用模型列表", font=font(12, "bold"), anchor="w"
                     ).grid(row=c1._next, column=0, sticky="ew", padx=18, pady=(10, 3))  # type: ignore[attr-defined]
        c1._next += 1  # type: ignore[attr-defined]
        self.tag_box = ctk.CTkFrame(c1, fg_color="transparent")
        self.tag_box.grid(row=c1._next, column=0, sticky="ew", padx=18)  # type: ignore[attr-defined]
        c1._next += 1  # type: ignore[attr-defined]

        add_row = ctk.CTkFrame(c1, fg_color="transparent")
        add_row.grid_columnconfigure(0, weight=1)
        self.ent_new_model = ctk.CTkEntry(add_row, height=34, corner_radius=9, font=font(12),
                                          placeholder_text="输入模型名后回车添加")
        self.ent_new_model.grid(row=0, column=0, sticky="ew")
        self.ent_new_model.bind("<Return>", lambda e: self._add_model())
        ctk.CTkButton(add_row, text="添加", width=62, height=34, corner_radius=9, font=font(12),
                      command=self._add_model).grid(row=0, column=1, padx=(8, 0))
        add_row.grid(row=c1._next, column=0, sticky="ew", padx=18, pady=(8, 0))  # type: ignore[attr-defined]
        c1._next += 1  # type: ignore[attr-defined]

        test_row = ctk.CTkFrame(c1, fg_color="transparent")
        ctk.CTkButton(test_row, text="测试连接", width=96, height=34, corner_radius=9,
                      font=font(12), command=self._test_connection).pack(side="left")
        self.lbl_test = ctk.CTkLabel(test_row, text="", font=font(12), anchor="w")
        self.lbl_test.pack(side="left", padx=12)
        test_row.grid(row=c1._next, column=0, sticky="ew", padx=18, pady=(14, 18))  # type: ignore[attr-defined]
        c1._next += 1  # type: ignore[attr-defined]

        # --- 网络与存储 ---
        c2 = card("网络与存储")
        g = ctk.CTkFrame(c2, fg_color="transparent")
        g.grid_columnconfigure((0, 1), weight=1)
        self.ent_timeout = ctk.CTkEntry(g, height=34, corner_radius=9, font=font(12))
        self.ent_proxy = ctk.CTkEntry(g, height=34, corner_radius=9, font=font(12),
                                      placeholder_text="http://127.0.0.1:7890")
        ctk.CTkLabel(g, text="超时（秒）", font=font(11), anchor="w",
                     text_color=("gray45", "gray60")).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkLabel(g, text="代理（可选）", font=font(11), anchor="w",
                     text_color=("gray45", "gray60")).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.ent_timeout.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(2, 0))
        self.ent_proxy.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(2, 0))
        field(c2, "超时与代理", g)

        self.chk_ssl = ctk.CTkCheckBox(c2, text="校验 SSL 证书（遇到自签证书报错时可关闭）",
                                       font=font(12), checkbox_width=17, checkbox_height=17)
        self.chk_ssl.grid(row=c2._next, column=0, sticky="w", padx=18, pady=(12, 0))  # type: ignore[attr-defined]
        c2._next += 1  # type: ignore[attr-defined]

        dir_row = ctk.CTkFrame(c2, fg_color="transparent")
        dir_row.grid_columnconfigure(0, weight=1)
        self.ent_savedir = ctk.CTkEntry(dir_row, height=34, corner_radius=9, font=font(12))
        self.ent_savedir.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(dir_row, text="选择", width=62, height=34, corner_radius=9, font=font(12),
                      command=self._pick_save_dir).grid(row=0, column=1, padx=(8, 0))
        ctk.CTkButton(dir_row, text="打开", width=62, height=34, corner_radius=9, font=font(12),
                      fg_color="transparent", border_width=1,
                      command=self._open_output_dir).grid(row=0, column=2, padx=(6, 0))
        field(c2, "图片保存目录", dir_row)
        ctk.CTkLabel(c2, text="", height=14).grid(row=c2._next, column=0)  # type: ignore[attr-defined]

        # --- 外观 ---
        c3 = card("外观")
        self.opt_theme = ctk.CTkOptionMenu(c3, values=["深色", "浅色"], height=34,
                                           corner_radius=9, font=font(12),
                                           command=self._set_theme_by_name)
        field(c3, "主题", self.opt_theme)
        ctk.CTkLabel(c3, text="", height=14).grid(row=c3._next, column=0)  # type: ignore[attr-defined]

        # --- 关于 ---
        c4 = card("关于")
        ctk.CTkLabel(c4, text=f"{APP_NAME} · 本地桌面工具\n"
                              f"所有请求由本机直接发往你配置的接口，不经过任何中间服务器。",
                     font=font(11), anchor="w", justify="left",
                     text_color=("gray45", "gray60")
                     ).grid(row=c4._next, column=0, sticky="ew", padx=18, pady=(0, 16))  # type: ignore[attr-defined]

        # --- 保存按钮 ---
        save_row = ctk.CTkFrame(page, fg_color="transparent")
        save_row.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self.btn_save = ctk.CTkButton(save_row, text="保存设置", height=44, corner_radius=10,
                                      font=font(14, "bold"), command=self._save_settings)
        self.btn_save.pack(fill="x", padx=4)

    # ================= 配置 <-> 表单 =================

    def _apply_config_to_form(self) -> None:
        c = self.cfg
        self.ent_base.insert(0, c.get("base_url", ""))
        self.ent_key.insert(0, c.get("api_key", ""))
        self.ent_default_model.insert(0, c.get("model", ""))
        self.ent_model.insert(0, c.get("model", ""))
        self.ent_timeout.insert(0, str(c.get("timeout", 300)))
        self.ent_proxy.insert(0, c.get("proxy", ""))
        self.ent_savedir.insert(0, c.get("save_dir", ""))
        self.chk_ssl.select() if c.get("verify_ssl", True) else self.chk_ssl.deselect()

        size = c.get("size", "1024x1024")
        if size in SIZE_OPTIONS:
            self.opt_size.set(size)
        else:
            self.opt_size.set("自定义…")
            self.ent_custom_size.grid()
            self.ent_custom_size.insert(0, size)
        self.ent_n.insert(0, str(c.get("n", 1)))
        self.opt_quality.set(c.get("quality") or "默认")
        self.opt_style.set(c.get("style") or "默认")
        self.opt_format.set(c.get("response_format") or "b64_json")
        self.txt_negative.insert("1.0", c.get("negative_prompt", ""))
        self.txt_extra.insert("1.0", c.get("extra_body", ""))
        self.opt_img2img.set("/images/edits（标准，推荐）")
        self.opt_theme.set("深色" if c.get("theme", "dark") == "dark" else "浅色")

        # ComfyUI
        self.ent_comfy_url.insert(0, c.get("comfy_url", "http://127.0.0.1:8188"))
        self.ent_comfy_timeout.insert(0, str(c.get("comfy_timeout", 600)))
        self._set_engine(self.engine, silent=True)
        wf = c.get("comfy_workflow") or ""
        if wf and os.path.isfile(wf):
            self._load_workflow_file(wf, notify=False)
        else:
            self.lbl_workflow.configure(text="尚未导入工作流")

    def _collect_settings(self) -> Dict[str, Any]:
        return {
            "base_url": self.ent_base.get().strip(),
            "api_key": self.ent_key.get().strip(),
            "model": self.ent_default_model.get().strip(),
            "timeout": int(self.ent_timeout.get() or 300),
            "proxy": self.ent_proxy.get().strip(),
            "save_dir": self.ent_savedir.get().strip(),
            "verify_ssl": bool(self.chk_ssl.get()),
            "theme": "dark" if self.opt_theme.get() == "深色" else "light",
            "models": self._model_tags,
            "engine": self.engine,
            "comfy_url": self.ent_comfy_url.get().strip(),
            "comfy_timeout": int(self.ent_comfy_timeout.get() or 600),
            "comfy_workflow": self._workflow_path,
            "comfy_mapping": self._comfy_mapping,
        }

    def _save_settings(self) -> None:
        try:
            patch = self._collect_settings()
        except ValueError:
            messagebox.showwarning(APP_NAME, "超时时间必须是数字。", parent=self)
            return
        if patch["save_dir"]:
            try:
                os.makedirs(patch["save_dir"], exist_ok=True)
            except OSError as exc:
                messagebox.showerror(APP_NAME, f"保存目录不可用：{exc}", parent=self)
                return
        self.cfg = cfg_mod.save_config(patch)
        # 同步生成页
        if not self.ent_model.get().strip():
            self.ent_model.delete(0, "end")
            self.ent_model.insert(0, patch["model"])
        ctk.set_appearance_mode(patch["theme"])
        self.btn_theme.configure(text=self._theme_icon())
        self._refresh_status_chip()
        self._status("设置已保存", "ok")

    def _toggle_key(self) -> None:
        if self.ent_key.cget("show") == "":
            self.ent_key.configure(show="•")
            self.btn_show_key.configure(text="显示")
        else:
            self.ent_key.configure(show="")
            self.btn_show_key.configure(text="隐藏")

    def _pick_save_dir(self) -> None:
        d = filedialog.askdirectory(parent=self, title="选择图片保存目录")
        if d:
            self.ent_savedir.delete(0, "end")
            self.ent_savedir.insert(0, os.path.normpath(d))

    def _set_theme_by_name(self, name: str) -> None:
        mode = "dark" if name == "深色" else "light"
        ctk.set_appearance_mode(mode)
        self.btn_theme.configure(text=self._theme_icon())

    # ================= 模型列表 =================

    def _refresh_model_tags(self) -> None:
        for w in self.tag_box.winfo_children():
            w.destroy()
        if not self._model_tags:
            ctk.CTkLabel(self.tag_box, text="暂无常用模型", font=font(11),
                         text_color=("gray50", "gray65")).pack(side="left")
        for m in self._model_tags:
            tag = ctk.CTkFrame(self.tag_box, corner_radius=14, height=30)
            tag.pack(side="left", padx=(0, 8), pady=3)
            ctk.CTkLabel(tag, text=m, font=font(12)).pack(side="left", padx=(10, 4))
            ctk.CTkButton(tag, text="×", width=22, height=22, corner_radius=11, font=font(12),
                          fg_color="transparent", hover_color=("#FEE2E2", "#4B2020"),
                          command=lambda mm=m: self._remove_model(mm)).pack(side="left", padx=(0, 6))
        self.opt_model.configure(values=self._model_tags or ["（常用模型）"])

    def _add_model(self) -> None:
        m = self.ent_new_model.get().strip()
        if not m or m in self._model_tags:
            return
        self._model_tags.append(m)
        self.ent_new_model.delete(0, "end")
        cfg_mod.save_config({"models": self._model_tags})
        self._refresh_model_tags()

    def _remove_model(self, m: str) -> None:
        if m in self._model_tags:
            self._model_tags.remove(m)
            cfg_mod.save_config({"models": self._model_tags})
            self._refresh_model_tags()

    def _pick_model(self, name: str) -> None:
        if name and not name.startswith("（"):
            self.ent_model.delete(0, "end")
            self.ent_model.insert(0, name)
            self._refresh_status_chip()

    # ================= 引擎切换 / ComfyUI =================

    def _on_engine_change(self, value: str) -> None:
        self.engine = "comfyui" if value.strip().lower().startswith("comfy") else "api"
        self._refresh_engine_layout()
        self._refresh_status_chip()

    def _refresh_engine_layout(self) -> None:
        if self.engine == "comfyui":
            self.frame_api.grid_remove()
            self.frame_comfy.grid()
        else:
            self.frame_comfy.grid_remove()
            self.frame_api.grid()

    def _set_engine(self, engine: str, silent: bool = False) -> None:
        self.engine = "comfyui" if engine == "comfyui" else "api"
        self.seg_engine.set("ComfyUI" if self.engine == "comfyui" else "API 中转站")
        self._refresh_engine_layout()
        self._refresh_status_chip()
        if not silent:
            cfg_mod.save_config({"engine": self.engine})

    def _import_workflow(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择 ComfyUI 工作流（API 格式）",
            filetypes=[("JSON 工作流", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        self._load_workflow_file(os.path.normpath(path), notify=True)

    def _load_workflow_file(self, path: str, notify: bool = False) -> bool:
        try:
            wf = ComfyUIClient.load_workflow(path)
        except Exception as exc:  # noqa: BLE001
            if notify:
                messagebox.showerror(APP_NAME, f"读取工作流失败：\n\n{exc}", parent=self)
            self._status(f"工作流读取失败：{exc}", "err")
            return False
        self._workflow = wf
        self._workflow_path = path
        self.lbl_workflow.configure(text=os.path.basename(path))
        self._analysis = ComfyUIClient.analyze(wf)
        self._refresh_mapping_menus()
        cfg_mod.save_config({"comfy_workflow": path})
        self._refresh_status_chip()
        if notify:
            self._status(f"已导入工作流：{os.path.basename(path)}（{len(wf)} 个节点）", "ok")
        return True

    def _refresh_mapping_menus(self) -> None:
        a = self._analysis or {}
        self._map_choices = {k: {} for k, _ in self._map_rows}

        def tag(node, title, extra=""):
            t = (title or "")[:16]
            return f"{node} · {t}" + (f" · {extra}" if extra else "")

        for c in a.get("text_candidates") or []:
            lb = tag(c["node"], c["title"], (c["text"] or "空")[:22])
            self._map_choices["positive"][lb] = {"node": c["node"], "input": c["input"]}
            self._map_choices["negative"][lb] = {"node": c["node"], "input": c["input"]}

        for c in a.get("size_candidates") or []:
            lb = tag(c["node"], c.get("title", ""), f"{c.get('w')}×{c.get('h')}")
            self._map_choices["size"][lb] = {
                "node": c["node"], "width_field": c["width"], "height_field": c["height"]}

        for c in a.get("batch_candidates") or []:
            lb = tag(c["node"], c.get("title", ""), f"批量 {c.get('value')}")
            self._map_choices["batch"][lb] = {"node": c["node"], "input": c["input"]}

        for c in a.get("seed_candidates") or []:
            lb = tag(c["node"], c.get("title", ""), f"种子 {c.get('value')}")
            self._map_choices["seed"][lb] = {"node": c["node"], "input": c["input"]}

        self._map_choices["image"]["（不使用参考图）"] = {}
        for c in a.get("image") or []:
            lb = tag(c["node"], c.get("title", ""), str(c.get("value")))
            self._map_choices["image"][lb] = {"node": c["node"], "input": c["input"]}

        for key, menu in self.map_menus.items():
            opts = list(self._map_choices[key].keys())
            if not opts:
                menu.configure(values=["（无可用节点）"], state="disabled")
                continue
            menu.configure(values=opts, state="normal")
            default = self._auto_choice(key)
            pick = default if default in opts else opts[0]
            menu.set(pick)
            self._comfy_select(key, pick)

    def _auto_choice(self, key: str) -> str:
        a = self._analysis or {}
        want = a.get(key) if key in ("positive", "negative", "size", "seed", "batch") else None
        if not want:
            return ""
        node = str(want.get("node") or "")
        for lb, payload in (self._map_choices.get(key) or {}).items():
            if str(payload.get("node")) == node:
                return lb
        return ""

    def _comfy_select(self, key: str, label: str) -> None:
        if not label or label.startswith("（"):
            if key == "image":
                self._comfy_mapping.pop("image_node", None)
                self._comfy_mapping.pop("image_field", None)
            return
        payload = (self._map_choices.get(key) or {}).get(label)
        if not payload:
            return
        m = self._comfy_mapping
        if key in ("positive", "negative"):
            m[f"{key}_node"] = payload["node"]
            m[f"{key}_input"] = payload["input"]
        elif key == "size":
            m["size_node"] = payload["node"]
            m["width_field"] = payload["width_field"]
            m["height_field"] = payload["height_field"]
        elif key == "seed":
            m["seed_node"] = payload["node"]
            m["seed_field"] = payload["input"]
        elif key == "batch":
            m["batch_node"] = payload["node"]
            m["batch_field"] = payload["input"]
        elif key == "image":
            m["image_node"] = payload["node"]
            m["image_field"] = payload["input"]

    def _test_comfy(self) -> None:
        url = self.ent_comfy_url.get().strip()
        self.lbl_comfy_state.configure(text="正在连接…", text_color=("gray50", "gray65"))
        self.update_idletasks()

        def worker():
            try:
                res = ComfyUIClient({"comfy_url": url}).test_connection()
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "message": str(exc)}
            self._post(self._on_comfy_test, res)

        threading.Thread(target=worker, daemon=True).start()

    def _on_comfy_test(self, res: Dict[str, Any]) -> None:
        if res.get("ok"):
            self.lbl_comfy_state.configure(text="✓ " + res.get("message", ""),
                                           text_color=("#15803D", "#4ADE80"))
        else:
            self.lbl_comfy_state.configure(text="✕ " + res.get("message", ""),
                                           text_color=("#B91C1C", "#F87171"))
        self._refresh_status_chip()

    # ================= 生成页交互 =================

    def _toggle_negative(self) -> None:
        if self.chk_neg.get():
            self.txt_negative.grid()
        else:
            self.txt_negative.grid_remove()

    def _toggle_advanced(self) -> None:
        if self.chk_adv.get():
            self.adv_frame.grid()
        else:
            self.adv_frame.grid_remove()

    def _on_size_change(self, value: str) -> None:
        if value == "自定义…":
            self.ent_custom_size.grid()
            self.ent_custom_size.focus_set()
        else:
            self.ent_custom_size.grid_remove()

    def _refresh_status_chip(self) -> None:
        if self.engine == "comfyui":
            url = self.ent_comfy_url.get().strip()
            if not url:
                self.lbl_chip.configure(text="未填写 ComfyUI 地址")
                self.lbl_chip_dot.configure(text_color=("#DC2626", "#F87171"))
                self.lbl_sub.configure(text="ComfyUI 未配置")
                return
            host = url.replace("https://", "").replace("http://", "").rstrip("/")
            if not self._workflow:
                self.lbl_chip.configure(text=f"ComfyUI · {host} · 未导入工作流")
                self.lbl_chip_dot.configure(text_color=("#DC2626", "#F87171"))
                self.lbl_sub.configure(text="ComfyUI 未就绪")
            else:
                self.lbl_chip.configure(text=f"ComfyUI · {host} · 已就绪")
                self.lbl_chip_dot.configure(text_color=("#16A34A", "#4ADE80"))
                self.lbl_sub.configure(text=f"ComfyUI · {host}")
            return

        base = self.ent_base.get().strip()
        key = self.ent_key.get().strip()
        model = self.ent_model.get().strip() or self.ent_default_model.get().strip()
        if not base:
            self.lbl_chip.configure(text="未填写 Base URL，请到「设置」中配置")
            self.lbl_chip_dot.configure(text_color=("#DC2626", "#F87171"))
            self.lbl_sub.configure(text="未配置")
        elif not key:
            self.lbl_chip.configure(text="未填写 API Key，请到「设置」中配置")
            self.lbl_chip_dot.configure(text_color=("#DC2626", "#F87171"))
            self.lbl_sub.configure(text="未配置密钥")
        else:
            host = base.replace("https://", "").replace("http://", "").split("/")[0]
            self.lbl_chip.configure(text=f"{host} · {model or '未选模型'}")
            self.lbl_chip_dot.configure(text_color=("#16A34A", "#4ADE80"))
            self.lbl_sub.configure(text=host)

    def _pick_reference(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择参考图",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp"), ("所有文件", "*.*")],
        )
        if path:
            self._set_reference(os.path.normpath(path))

    def _set_reference(self, path: Optional[str]) -> None:
        self.ref_image = path
        if not path:
            self.lbl_ref_img.configure(image=None, text="")
            self.lbl_ref.configure(text="未选择参考图\n不选即为「文生图」")
            return
        try:
            im = Image.open(path)
            im.thumbnail((64, 64))
            cim = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
            self._img_refs["__ref__"] = cim
            self.lbl_ref_img.configure(image=cim, text="")
            self.lbl_ref.configure(text=f"已选择：{os.path.basename(path)}\n本次将使用图生图模式")
        except Exception as exc:  # noqa: BLE001
            self._status(f"参考图读取失败：{exc}", "err")

    def _clear_reference(self) -> None:
        self._set_reference(None)

    def _current_size(self) -> str:
        v = self.opt_size.get()
        if v == "自定义…":
            return self.ent_custom_size.get().strip()
        return v

    # ================= 生成流程 =================

    def on_generate(self) -> None:
        if self.generating:
            return
        prompt = self.txt_prompt.get("1.0", "end").strip()
        if not prompt:
            messagebox.showwarning(APP_NAME, "请先填写提示词。", parent=self)
            self.txt_prompt.focus_set()
            return

        size = self._current_size()
        if not size:
            messagebox.showwarning(APP_NAME, "请填写自定义尺寸，例如 1024x1024。", parent=self)
            return
        try:
            n = max(1, min(int(self.ent_n.get() or 1), 10))
        except ValueError:
            messagebox.showwarning(APP_NAME, "数量必须是 1-10 的整数。", parent=self)
            return
        seed = self.ent_seed.get().strip()
        if seed and not seed.lstrip("-").isdigit():
            messagebox.showwarning(APP_NAME, "种子必须是整数，或留空。", parent=self)
            return

        if self.engine == "comfyui":
            self._start_comfy(prompt, size, n, seed)
        else:
            self._start_api(prompt, size, n, seed)

    # ---- 启动：API 中转站 ----
    def _start_api(self, prompt: str, size: str, n: int, seed: str) -> None:
        base = self.ent_base.get().strip()
        key = self.ent_key.get().strip()
        model = self.ent_model.get().strip() or self.ent_default_model.get().strip()
        if not base or not key:
            messagebox.showwarning(APP_NAME, "请先到「设置」中填写 Base URL 与 API Key。", parent=self)
            self._show_page("settings")
            return
        if not model:
            messagebox.showwarning(APP_NAME, "请填写模型名。", parent=self)
            return

        extra = self.txt_extra.get("1.0", "end").strip()
        if extra:
            try:
                import json as _json
                _json.loads(extra)
            except Exception:  # noqa: BLE001
                messagebox.showwarning(APP_NAME, "附加请求体不是合法 JSON。", parent=self)
                return

        cfg = cfg_mod.load_config()
        cfg.update({
            "base_url": base, "api_key": key, "model": model,
            "timeout": int(self.ent_timeout.get() or 300)
            if self.ent_timeout.get().strip().isdigit() else cfg.get("timeout", 300),
            "proxy": self.ent_proxy.get().strip(),
            "verify_ssl": bool(self.chk_ssl.get()),
            "extra_body": extra,
        })

        payload = {
            "prompt": prompt,
            "negative_prompt": self.txt_negative.get("1.0", "end").strip(),
            "size": size,
            "n": n,
            "quality": "" if self.opt_quality.get() == "默认" else self.opt_quality.get(),
            "style": "" if self.opt_style.get() == "默认" else self.opt_style.get(),
            "seed": seed,
            "response_format": "" if self.opt_format.get() == "不指定" else self.opt_format.get(),
            "reference_image": self.ref_image,
            "img2img_mode": "generations"
            if self.opt_img2img.get().startswith("/images/generations") else "edits",
        }

        self._begin_generating("正在请求接口…")
        threading.Thread(target=self._generate_worker, args=(cfg, payload), daemon=True).start()

    # ---- 启动：ComfyUI ----
    def _start_comfy(self, prompt: str, size: str, n: int, seed: str) -> None:
        url = self.ent_comfy_url.get().strip()
        if not url:
            messagebox.showwarning(APP_NAME, "请填写 ComfyUI 服务地址。", parent=self)
            return
        if not self._workflow:
            messagebox.showwarning(
                APP_NAME,
                "还没有导入 ComfyUI 工作流。\n\n"
                "请在 ComfyUI 里搭好流程，用「Workflow → Export (API)」导出 JSON，\n"
                "再回到这里点「导入 JSON」。",
                parent=self)
            return
        try:
            w, h = self._parse_size(size)
        except ValueError:
            messagebox.showwarning(APP_NAME, "尺寸格式不对，应为「宽x高」，例如 1024x1024。", parent=self)
            return

        cfg = cfg_mod.load_config()
        cfg.update({
            "comfy_url": url,
            "comfy_timeout": int(self.ent_comfy_timeout.get() or 600)
            if self.ent_comfy_timeout.get().strip().isdigit() else 600,
            "comfy_workflow": self._workflow_path,
            "comfy_mapping": dict(self._comfy_mapping),
        })

        payload = {
            "prompt": prompt,
            "negative_prompt": self.txt_negative.get("1.0", "end").strip(),
            "width": w, "height": h, "n": n, "seed": seed,
            "workflow": self._workflow,
            "mapping": dict(self._comfy_mapping),
            "reference_image": self.ref_image if self._comfy_mapping.get("image_node") else None,
        }

        self._begin_generating("正在提交任务…")
        threading.Thread(target=self._comfy_worker, args=(cfg, payload), daemon=True).start()

    @staticmethod
    def _parse_size(size: str) -> Tuple[int, int]:
        parts = str(size).lower().replace("×", "x").replace("*", "x").split("x")
        if len(parts) != 2:
            raise ValueError(size)
        w, h = int(parts[0].strip()), int(parts[1].strip())
        if w <= 0 or h <= 0:
            raise ValueError(size)
        return w, h

    def _begin_generating(self, msg: str) -> None:
        self.generating = True
        self.btn_generate.configure(state="disabled", text="生成中…")
        self.progress.grid(row=0, column=0, sticky="ew", pady=(6, 0))
        self.progress.set(0.03)
        self.lbl_progress.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.lbl_progress.configure(text=msg)
        self._status("正在生成…")

    def _comfy_worker(self, cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
        try:
            save_dir = cfg_mod.output_dir(cfg)
            os.makedirs(save_dir, exist_ok=True)
            client = ComfyUIClient(cfg)
            started = time.time()
            result = client.generate(
                prompt=payload["prompt"],
                negative_prompt=payload["negative_prompt"],
                width=payload["width"],
                height=payload["height"],
                n=payload["n"],
                seed=payload["seed"],
                workflow=payload["workflow"],
                mapping=payload["mapping"],
                save_dir=save_dir,
                reference_image=payload["reference_image"],
                progress=lambda d, t, m: self._post(self._on_progress, d, t, m),
            )
            if not result.get("ok"):
                self._post(self._on_generate_failed, result.get("error", "未知错误"))
                return

            record = hist_mod.add_record(
                prompt=payload["prompt"],
                negative_prompt=payload["negative_prompt"],
                params={
                    "model": "ComfyUI",
                    "base_url": cfg.get("comfy_url", ""),
                    "size": f'{payload["width"]}x{payload["height"]}',
                    "n": payload["n"], "quality": "", "style": "",
                    "seed": payload["seed"],
                },
                images=result["images"],
                mode="img2img" if payload["reference_image"] else "text2img",
                elapsed=result.get("elapsed", round(time.time() - started, 2)),
                warnings=result.get("warnings", []),
            )
            self._post(self._on_generate_done, record, result.get("warnings", []))
        except Exception as exc:  # noqa: BLE001
            self._post(self._on_generate_failed, f"{type(exc).__name__}: {exc}")

    def _generate_worker(self, cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
        try:
            save_dir = cfg_mod.output_dir(cfg)
            os.makedirs(save_dir, exist_ok=True)
            client = ImageAPIClient(cfg)
            started = time.time()
            result = client.generate(
                payload["prompt"],
                negative_prompt=payload["negative_prompt"],
                size=payload["size"],
                n=payload["n"],
                model=cfg["model"],
                quality=payload["quality"],
                style=payload["style"],
                seed=payload["seed"],
                response_format=payload["response_format"],
                reference_image=payload["reference_image"],
                img2img_mode=payload["img2img_mode"],
                save_dir=save_dir,
                progress=lambda d, t, m: self._post(self._on_progress, d, t, m),
            )
            if not result.get("ok"):
                self._post(self._on_generate_failed, result.get("error", "未知错误"))
                return
            record = hist_mod.add_record(
                prompt=payload["prompt"],
                negative_prompt=payload["negative_prompt"],
                params={
                    "model": cfg["model"], "base_url": cfg.get("base_url", ""),
                    "size": payload["size"], "n": payload["n"],
                    "quality": payload["quality"], "style": payload["style"],
                    "seed": payload["seed"],
                },
                images=result["images"],
                mode="img2img" if payload["reference_image"] else "text2img",
                elapsed=result.get("elapsed", round(time.time() - started, 2)),
                warnings=result.get("warnings", []),
            )
            self._post(self._on_generate_done, record, result.get("warnings", []))
        except Exception as exc:  # noqa: BLE001
            self._post(self._on_generate_failed, f"{type(exc).__name__}: {exc}")

    def _on_progress(self, done: int, total: int, msg: str) -> None:
        total = max(1, total)
        self.progress.set(min(1.0, max(0.03, done / total)))
        self.lbl_progress.configure(text=msg or "处理中…")

    def _on_generate_failed(self, err: str) -> None:
        self._finish_generating()
        self._status(f"生成失败：{err}", "err")
        messagebox.showerror(APP_NAME, f"生成失败：\n\n{err}", parent=self)

    def _on_generate_done(self, record: Dict[str, Any], warnings: List[str]) -> None:
        self._finish_generating()
        self.lbl_empty.grid_remove()
        self.last_images = list(record.get("images", []))
        self._render_record_cards(record)
        self.btn_export.pack(side="right", padx=(0, 8))
        self._status(f"生成完成，用时 {record.get('elapsed')}s，共 {len(record['images'])} 张", "ok")
        for w in warnings:
            self._status(f"提示：{w}", "warn")

    def _finish_generating(self) -> None:
        self.generating = False
        self.btn_generate.configure(state="normal", text="开始生成")
        self.progress.grid_remove()
        self.lbl_progress.grid_remove()

    def _render_record_cards(self, record: Dict[str, Any]) -> None:
        existing = getattr(self, "_cards", 0)
        for i, img in enumerate(record.get("images", [])):
            self._add_card(self.gallery, img, existing + i)
        self._cards = existing + len(record.get("images", []))
        self.lbl_result_meta.configure(text=f"累计 {self._cards} 张")

    def _add_card(self, parent, img: Dict[str, Any], index: int) -> None:
        row, col = divmod(index, 3)
        card = ctk.CTkFrame(parent, corner_radius=10)
        card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)
        card.grid_columnconfigure(0, weight=1)

        try:
            im = Image.open(img.get("thumb") or img["path"])
            im = im.convert("RGB")
            im.thumbnail((THUMB_SIZE, THUMB_SIZE))
            cim = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
            self._img_refs[img["path"]] = cim
        except Exception:  # noqa: BLE001
            cim = None

        btn = ctk.CTkButton(
            card, text="", image=cim, height=THUMB_SIZE, corner_radius=8,
            fg_color=("gray88", "gray18"), hover=False,
            command=lambda i=img: self._preview(i),
        )
        btn.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 4))

        foot = ctk.CTkFrame(card, fg_color="transparent")
        foot.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
        ctk.CTkLabel(foot, text=img["filename"], font=font(10),
                     text_color=("gray45", "gray60"), anchor="w"
                     ).pack(side="left", expand=True, fill="x")
        ctk.CTkButton(foot, text="打开", width=44, height=24, corner_radius=6, font=font(11),
                      fg_color="transparent", border_width=1,
                      command=lambda p=img["path"]: self._open_file(p)).pack(side="right")
        ctk.CTkButton(foot, text="位置", width=44, height=24, corner_radius=6, font=font(11),
                      fg_color="transparent", border_width=1,
                      command=lambda p=img["path"]: self._reveal(p)).pack(side="right", padx=(0, 4))

    def _clear_results(self) -> None:
        for w in self.gallery.winfo_children():
            if w is not self.lbl_empty:
                w.destroy()
        self._cards = 0
        self.last_images = []
        self.lbl_result_meta.configure(text="")
        self.lbl_empty.grid(row=0, column=0, columnspan=3, pady=90)
        self.btn_export.pack_forget()

    def _export_all(self) -> None:
        import shutil
        if not self.last_images:
            return
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.isdir(desktop):
            desktop = os.path.join(os.path.expanduser("~"), "桌面")
        target = os.path.join(desktop, "API生图导出_" + time.strftime("%Y%m%d_%H%M%S"))
        try:
            os.makedirs(target, exist_ok=True)
            n = 0
            for img in self.last_images:
                p = img.get("path")
                if p and os.path.isfile(p):
                    shutil.copy2(p, os.path.join(target, os.path.basename(p)))
                    n += 1
            self._status(f"已导出 {n} 张到 {target}", "ok")
            os.startfile(target)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"导出失败：{exc}", parent=self)

    # ================= 预览 =================

    def _preview(self, img: Dict[str, Any]) -> None:
        top = ctk.CTkToplevel(self)
        top.title(img.get("filename", "预览"))
        top.geometry("1020x780")
        try:
            im = Image.open(img["path"])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"无法打开图片：{exc}", parent=self)
            top.destroy()
            return
        im.thumbnail(PREVIEW_MAX)
        cim = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
        self._img_refs["__preview__"] = cim
        ctk.CTkLabel(top, text="", image=cim).pack(expand=True, fill="both", padx=16, pady=(16, 8))
        bar = ctk.CTkFrame(top, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkLabel(bar, text=f"{img['filename']}   {im.size[0]}×{im.size[1]}",
                     font=font(12), text_color=("gray45", "gray60")).pack(side="left")
        ctk.CTkButton(bar, text="在文件夹中显示", width=120, height=32, corner_radius=8,
                      font=font(12), fg_color="transparent", border_width=1,
                      command=lambda: self._reveal(img["path"])).pack(side="right")
        ctk.CTkButton(bar, text="用默认程序打开", width=120, height=32, corner_radius=8,
                      font=font(12), command=lambda: self._open_file(img["path"])).pack(side="right", padx=8)

    # ================= 历史 =================

    def load_history(self) -> None:
        self.records = hist_mod.load_history()
        self._render_history()

    def _render_history(self) -> None:
        for w in self.hist_scroll.winfo_children():
            w.destroy()
        self.lbl_hist_meta.configure(text=f"共 {len(self.records)} 条记录")
        if not self.records:
            ctk.CTkLabel(self.hist_scroll, text="还没有历史记录\n\n每次生成都会自动保存在这里",
                         font=font(13), text_color=("gray55", "gray50"), justify="center"
                         ).grid(row=0, column=0, pady=80)
            return
        for i, rec in enumerate(self.records):
            self._render_history_card(rec, i)

    def _render_history_card(self, rec: Dict[str, Any], row: int) -> None:
        card = ctk.CTkFrame(self.hist_scroll, corner_radius=11)
        card.grid(row=row, column=0, sticky="ew", padx=6, pady=7)
        card.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        ctk.CTkLabel(top, text=rec.get("prompt", ""), font=font(13), anchor="w",
                     justify="left", wraplength=900
                     ).pack(side="left", expand=True, fill="x")
        ctk.CTkButton(top, text="复用", width=58, height=28, corner_radius=7, font=font(11),
                      fg_color="transparent", border_width=1,
                      command=lambda r=rec: self._reuse(r)).pack(side="right")
        ctk.CTkButton(top, text="删除", width=58, height=28, corner_radius=7, font=font(11),
                      fg_color="transparent", border_width=1,
                      hover_color=("#FEE2E2", "#4B2020"),
                      command=lambda r=rec: self._delete_record(r)).pack(side="right", padx=6)

        tags = ctk.CTkFrame(card, fg_color="transparent")
        tags.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        info = [
            ("图生图" if rec.get("mode") == "img2img" else "文生图"),
            rec.get("params", {}).get("model", ""),
            rec.get("params", {}).get("size", ""),
            f"{len(rec.get('images', []))} 张",
            f"{rec.get('elapsed', '')}s",
            rec.get("time", ""),
        ]
        for t in info:
            if t:
                ctk.CTkLabel(tags, text=str(t), font=font(10),
                             text_color=("gray40", "gray65"),
                             fg_color=("gray90", "gray22"), corner_radius=8
                             ).pack(side="left", padx=(0, 6), ipadx=7, ipady=1)

        thumbs = ctk.CTkFrame(card, fg_color="transparent")
        thumbs.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 14))
        for img in rec.get("images", [])[:8]:
            try:
                im = Image.open(img.get("thumb") or img["path"]).convert("RGB")
                im.thumbnail((92, 92))
                cim = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
                key = f"{rec.get('id')}:{img['path']}"
                self._img_refs[key] = cim
            except Exception:  # noqa: BLE001
                continue
            ctk.CTkButton(thumbs, text="", image=cim, width=92, height=92, corner_radius=7,
                          fg_color=("gray88", "gray18"), hover=False,
                          command=lambda i=img: self._preview(i)).pack(side="left", padx=(0, 8))

    def _reuse(self, rec: Dict[str, Any]) -> None:
        self.txt_prompt.delete("1.0", "end")
        self.txt_prompt.insert("1.0", rec.get("prompt", ""))
        neg = rec.get("negative_prompt", "")
        self.txt_negative.delete("1.0", "end")
        if neg:
            self.txt_negative.insert("1.0", neg)
            if not self.chk_neg.get():
                self.chk_neg.select()
                self.txt_negative.grid()
        p = rec.get("params", {})
        # ComfyUI 的记录会把 model 记成 "ComfyUI"，不要填进模型输入框
        if p.get("model") and p["model"] != "ComfyUI":
            self.ent_model.delete(0, "end")
            self.ent_model.insert(0, p["model"])
        if p.get("size"):
            if p["size"] in SIZE_OPTIONS:
                self.opt_size.set(p["size"])
                self.ent_custom_size.grid_remove()
            else:
                self.opt_size.set("自定义…")
                self.ent_custom_size.grid()
                self.ent_custom_size.delete(0, "end")
                self.ent_custom_size.insert(0, p["size"])
        if p.get("quality"):
            self.opt_quality.set(p["quality"])
        if p.get("style"):
            self.opt_style.set(p["style"])
        self._show_page("create")
        self._refresh_status_chip()
        self._status("已把历史记录中的提示词与参数填入生成页", "ok")

    def _delete_record(self, rec: Dict[str, Any]) -> None:
        if not messagebox.askyesno(APP_NAME, "确定删除这条历史记录吗？（图片文件不会被删除）", parent=self):
            return
        hist_mod.delete_record(rec.get("id", ""))
        self.load_history()
        self._status("已删除该条历史记录", "ok")

    def _clear_history(self) -> None:
        if not self.records:
            return
        if not messagebox.askyesno(APP_NAME, "确定清空全部历史记录吗？（图片文件不会被删除）", parent=self):
            return
        hist_mod.clear_history()
        self.load_history()
        self._status("历史记录已清空", "ok")

    # ================= 连接测试 =================

    def _test_connection(self) -> None:
        self.lbl_test.configure(text="测试中…", text_color=("gray45", "gray60"))
        self.update_idletasks()
        cfg = cfg_mod.load_config()
        cfg.update({
            "base_url": self.ent_base.get().strip(),
            "api_key": self.ent_key.get().strip(),
            "timeout": 30,
            "proxy": self.ent_proxy.get().strip(),
            "verify_ssl": bool(self.chk_ssl.get()),
        })

        def worker():
            try:
                res = ImageAPIClient(cfg).test_connection()
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "message": str(exc)}
            self._post(self._on_test_done, res)

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, res: Dict[str, Any]) -> None:
        if res.get("ok"):
            self.lbl_test.configure(text="✓ " + res.get("message", ""),
                                    text_color=("#15803D", "#4ADE80"))
        else:
            self.lbl_test.configure(text="✕ " + res.get("message", ""),
                                    text_color=("#B91C1C", "#F87171"))

    # ================= 系统操作 =================

    def _open_output_dir(self) -> None:
        path = cfg_mod.output_dir()
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"打开目录失败：{exc}", parent=self)

    def _open_file(self, path: str) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"打开失败：{exc}", parent=self)

    def _reveal(self, path: str) -> None:
        try:
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"定位失败：{exc}", parent=self)

    def _on_close(self) -> None:
        if self.generating and not messagebox.askyesno(
            APP_NAME, "正在生成中，确定要退出吗？", parent=self
        ):
            return
        self.destroy()


# ---------------------------------------------------------------------------

def _log_path() -> str:
    """错误日志优先写到 exe 旁边（好找），不行再退回数据目录。"""
    candidates = []
    if cfg_mod.is_frozen():
        candidates.append(os.path.dirname(os.path.abspath(sys.executable)))
    candidates.append(cfg_mod.ROOT_DIR)
    for folder in candidates:
        try:
            os.makedirs(folder, exist_ok=True)
            probe = os.path.join(folder, ".write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            return os.path.join(folder, "启动错误.log")
        except OSError:
            continue
    return ""


def show_fatal_error(text: str) -> None:
    """启动失败时给出可见提示（无控制台的打包环境也能看到）。"""
    log = _log_path()
    if log:
        try:
            with open(log, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            log = ""
    tip = f"\n\n详细信息已写入：\n{log}" if log else ""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text[:1500] + tip, f"{APP_NAME} · 启动失败", 0x10)
    except Exception:  # noqa: BLE001
        print(text)


def _cleanup_strays() -> None:
    """清理卸载程序留下的临时文件（运行时它被锁，退出后即可删除）。

    打包后 tempfile.gettempdir() 可能指向 PyInstaller 自己的临时目录，
    因此把所有可能的临时目录都扫一遍。
    """
    import tempfile
    bases = set()
    for cand in (
        tempfile.gettempdir(),
        os.environ.get("TEMP"),
        os.environ.get("TMP"),
        os.path.join(os.environ.get("LOCALAPPDATA") or "", "Temp"),
    ):
        if cand:
            try:
                bases.add(os.path.abspath(cand))
            except OSError:
                pass

    # 注意：必须与安装程序使用的名称一致（APP_DIR_NAME 不带空格，
    # 而界面标题 APP_NAME 是带空格的展示名）
    prefix = f"{cfg_mod.APP_DIR_NAME}_uninstall_"
    for base in bases:
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for f in names:
            if f.startswith(prefix) and f.endswith(".exe"):
                try:
                    os.remove(os.path.join(base, f))
                except OSError:
                    pass


def run() -> None:
    """启动程序（供 __main__.py 与打包入口调用）。"""
    cfg_mod.ensure_dirs()
    _cleanup_strays()
    app = App()
    app.mainloop()
