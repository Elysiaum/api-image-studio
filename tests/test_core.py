# -*- coding: utf-8 -*-
"""后端与界面自检脚本（不发起真实生图请求）。"""
import base64
import json
import os
import sys

try:  # 重定向到文件时也保证逐行输出
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from api_image_studio.core.api_client import ImageAPIClient, build_endpoint, _iter_data_items, _extract_error  # noqa
from api_image_studio.core import config as cfg_mod
from api_image_studio.core import history as hist_mod

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(name)


print("== 1. Base URL 拼接 ==")
cases = [
    ("https://api.openai.com", "/images/generations", "https://api.openai.com/v1/images/generations"),
    ("https://api.openai.com/v1", "/images/generations", "https://api.openai.com/v1/images/generations"),
    ("https://api.openai.com/v1/", "/images/edits", "https://api.openai.com/v1/images/edits"),
    ("https://relay.xx.com", "/images/edits", "https://relay.xx.com/v1/images/edits"),
    # 已带版本号（v3）时不应再追加 /v1
    ("https://relay.xx.com/api/v3", "/images/generations", "https://relay.xx.com/api/v3/images/generations"),
    ("https://relay.xx.com/v1/images/generations", "/images/generations",
     "https://relay.xx.com/v1/images/generations"),
]
for base, path, expect in cases:
    got = build_endpoint(base, path)
    check(f"{base} -> {got}", got == expect, "" if got == expect else f"期望 {expect}")

print("\n== 2. 返回结构解析 ==")
check("标准 data/b64_json", len(_iter_data_items({"data": [{"b64_json": "x"}]})) == 1)
check("标准 data/url", len(_iter_data_items({"data": [{"url": "http://a/b.png"}]})) == 1)
check("非标准 images", len(_iter_data_items({"images": [{"url": "u"}]})) == 1)
check("裸对象", len(_iter_data_items({"b64_json": "x"})) == 1)
check("空返回", len(_iter_data_items({"data": []})) == 0)

print("\n== 3. 错误信息提取 ==")
class FakeResp:
    def __init__(self, payload, status=400):
        self._p = payload
        self.status_code = status
        self.text = json.dumps(payload)
    def json(self):
        return self._p

check("error.message", "余额不足" in _extract_error(FakeResp({"error": {"message": "余额不足"}})))
check("error 字符串", "bad key" in _extract_error(FakeResp({"error": "bad key"})))

print("\n== 4. 参数自动降级 ==")
payload = {"model": "m", "prompt": "p", "n": 1, "response_format": "b64_json", "quality": "hd"}
dropped = []
changed = ImageAPIClient._drop_offending(payload, "Unsupported parameter: 'response_format'", dropped)
check("识别并移除不支持字段", changed and "response_format" not in payload, str(payload))
check("保留合法字段", "quality" in payload)
check("无关报错不误删", not ImageAPIClient._drop_offending(payload, "something totally different", dropped))

print("\n== 5. 配置读写 ==")
cfg_mod.ensure_dirs()
cfg_mod.save_config({"model": "self-test-model"})
check("写入后读回", cfg_mod.load_config()["model"] == "self-test-model")
check("Key 打码", cfg_mod.mask_key("sk-1234567890abcdef") == "sk-123...cdef",
      cfg_mod.mask_key("sk-1234567890abcdef"))
cfg_mod.save_config({"model": cfg_mod.DEFAULT_CONFIG["model"]})

print("\n== 6. 图片落盘 / 缩略图 / 历史 ==")
tiny_png = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001a5f645400000000049454e44ae426082"
)).decode()
client = ImageAPIClient({"base_url": "https://x.com/v1", "api_key": "k"})
saved_img = client._save_item({"b64_json": tiny_png}, cfg_mod.output_dir(), "selftest", 1)
check("b64 落盘", bool(saved_img) and os.path.isfile(saved_img["path"]),
      saved_img["filename"] if saved_img else "None")

thumb_ok = False
if saved_img:
    th = hist_mod.make_thumbnail(saved_img["path"])
    thumb_ok = os.path.isfile(th)
check("缩略图生成", thumb_ok)

before = len(hist_mod.load_history())
rec = hist_mod.add_record(
    prompt="自检", negative_prompt="", params={"model": "m"},
    images=[{"path": saved_img["path"], "filename": "a.png", "url": "",
             "revised_prompt": "", "size_bytes": 1}] if saved_img else [],
    mode="text2img", elapsed=1.0,
)
check("新增历史记录", len(hist_mod.load_history()) == before + 1)
check("删除历史记录", hist_mod.delete_record(rec["id"]))
check("删除后数量还原", len(hist_mod.load_history()) == before)
if saved_img:
    os.remove(saved_img["path"])

print("\n== 7. 界面可构建（tkinter 冒烟测试）==")
try:
    from api_image_studio import gui as app_mod
    import customtkinter as ctk
    inst = app_mod.App()
    inst.update()
    checks = {
        "顶部标签": len(inst.tab_buttons) == 3,
        "三个页面": set(inst.pages) == {"create", "history", "settings"},
        "提示词框": inst.txt_prompt.winfo_exists() == 1,
        "生成按钮": inst.btn_generate.cget("text") == "开始生成",
        "参数下拉": inst.opt_size.get() in app_mod.SIZE_OPTIONS,
        "模型列表渲染": inst.tag_box.winfo_children() is not None,
    }
    for k, v in checks.items():
        check(k, bool(v))
    # 页面切换
    inst._show_page("history")
    inst.update()
    check("切换到历史页", inst.pages["history"].winfo_ismapped() == 1)
    inst._show_page("settings")
    inst.update()
    check("切换到设置页", inst.pages["settings"].winfo_ismapped() == 1)
    inst._show_page("create")
    inst.update()
    check("切回生成页", inst.pages["create"].winfo_ismapped() == 1)
    # 主题切换
    before_theme = ctk.get_appearance_mode()
    inst._toggle_theme()
    check("主题切换", ctk.get_appearance_mode() != before_theme,
          f"{before_theme} -> {ctk.get_appearance_mode()}")
    inst._toggle_theme()
    # 参考图设置与移除
    if saved_img:
        pass
    inst._set_reference(None)
    check("参考图移除后回到文生图", inst.ref_image is None)
    inst.destroy()
except Exception as exc:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    check("界面构建", False, str(exc))

print("\n" + "=" * 46)
if fails:
    print(f"结果：{len(fails)} 项未通过 -> {fails}")
    sys.exit(1)
print("结果：全部通过 ✓")
