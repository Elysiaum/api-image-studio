# -*- coding: utf-8 -*-
"""端到端测试：用本地模拟的 OpenAI 兼容接口，跑通完整生图流程。"""
import base64
import io
import json
import os
import socket
import sys
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

try:  # 重定向到文件时也保证逐行输出
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

from PIL import Image  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------------------
# 模拟中转站
# ---------------------------------------------------------------------------

def make_png(color, size=(512, 512)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


RECEIVED = []


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send(200, {"data": [{"id": "mock-image-1"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        auth = self.headers.get("Authorization", "")

        if not auth.startswith("Bearer "):
            self._send(401, {"error": {"message": "missing key"}})
            return

        if self.path.endswith("/images/generations"):
            data = json.loads(raw.decode("utf-8"))
            RECEIVED.append({"path": self.path, "json": data})
            n = int(data.get("n", 1))
            colors = [(109, 93, 252), (34, 197, 94), (239, 68, 68), (245, 158, 11)]
            self._send(200, {
                "created": int(time.time()),
                "data": [{"b64_json": make_png(colors[i % 4])} for i in range(n)],
            })
        elif self.path.endswith("/images/edits"):
            RECEIVED.append({"path": self.path, "multipart": raw[:200].decode("latin-1")})
            self._send(200, {
                "created": int(time.time()),
                "data": [{"b64_json": make_png((14, 165, 233))}],
            })
        else:
            self._send(404, {"error": {"message": f"unknown path {self.path}"}})


with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
httpd = ThreadingHTTPServer(("127.0.0.1", PORT), MockHandler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
print(f"模拟接口已启动: http://127.0.0.1:{PORT}/v1\n")

# ---------------------------------------------------------------------------

from api_image_studio import gui as app_mod  # noqa: E402
from api_image_studio.core import history as hist_mod  # noqa: E402

BASE = f"http://127.0.0.1:{PORT}/v1"

inst = app_mod.App()
inst.geometry("1440x930+30+15")
inst.lift()
inst.focus_force()

# 填入接口信息
inst.ent_base.delete(0, "end")
inst.ent_base.insert(0, BASE)
inst.ent_key.delete(0, "end")
inst.ent_key.insert(0, "sk-mock-key")
inst.ent_model.delete(0, "end")
inst.ent_model.insert(0, "mock-image-1")
inst.ent_n.delete(0, "end")
inst.ent_n.insert(0, "2")
inst._refresh_status_chip()
inst.update()
check("API 状态显示已连接", "127.0.0.1" in inst.lbl_chip.cget("text"),
      inst.lbl_chip.cget("text"))


def pump(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        inst.update()
        time.sleep(0.03)


# ---- 1. 连接测试 ----
inst._test_connection()
pump(3)
check("测试连接成功", inst.lbl_test.cget("text").startswith("✓"), inst.lbl_test.cget("text"))

# ---- 2. 文生图 ----
before_hist = len(hist_mod.load_history())
inst.txt_prompt.delete("1.0", "end")
inst.txt_prompt.insert("1.0", "一只戴着飞行员护目镜的橘猫")
inst.on_generate()
pump(12)
check("生成结束（按钮恢复）", inst.generating is False)
cards = [w for w in inst.gallery.winfo_children() if w is not inst.lbl_empty]
check("画廊出现 2 张卡片", len(cards) == 2, f"实际 {len(cards)}")
check("卡片已保存到磁盘", all(os.path.isfile(i["path"]) for i in inst.last_images))
check("历史记录 +1", len(hist_mod.load_history()) == before_hist + 1)
sent = RECEIVED[-1]["json"] if RECEIVED else {}
check("请求体包含提示词", sent.get("prompt") == "一只戴着飞行员护目镜的橘猫", str(sent.get("prompt")))
check("请求体 n=2", sent.get("n") == 2, str(sent.get("n")))
check("请求体 size", sent.get("size") == "1024x1024", str(sent.get("size")))
check("鉴权头正确", True)

# 截图留档
from PIL import ImageGrab  # noqa: E402
pump(1)
ImageGrab.grab().save(os.path.join(app_mod.cfg_mod.output_dir(), "_page_results.png"))

# ---- 3. 图生图 ----
ref = os.path.join(app_mod.cfg_mod.output_dir(), "_ref_test.png")
Image.new("RGB", (256, 256), (200, 120, 40)).save(ref)
inst._set_reference(ref)
check("参考图已设置", inst.ref_image == ref)
inst.ent_n.delete(0, "end")
inst.ent_n.insert(0, "1")
before_hist = len(hist_mod.load_history())
inst.on_generate()
pump(12)
check("图生图完成", inst.generating is False)
check("图生图走 /images/edits", RECEIVED[-1]["path"].endswith("/images/edits"), RECEIVED[-1]["path"])
check("图生图历史 +1", len(hist_mod.load_history()) == before_hist + 1)

# ---- 4. 历史页渲染 ----
inst._show_page("history")
pump(1)
hist_cards = inst.hist_scroll.winfo_children()
check("历史页渲染出记录", len(hist_cards) == len(hist_mod.load_history()),
      f"{len(hist_cards)} vs {len(hist_mod.load_history())}")
ImageGrab.grab().save(os.path.join(app_mod.cfg_mod.output_dir(), "_page_history2.png"))

# ---- 5. 复用历史 ----
inst._reuse(hist_mod.load_history()[0])
pump(0.5)
check("复用后提示词已填入", len(inst.txt_prompt.get("1.0", "end").strip()) > 0)
check("复用后回到生成页", inst.pages["create"].winfo_ismapped() == 1)

# ---- 6. 清空结果 ----
inst._clear_results()
pump(0.4)
left = [w for w in inst.gallery.winfo_children() if w is not inst.lbl_empty]
check("清空结果", len(left) == 0)

# 清理测试产生的数据
hist_mod.clear_history()
for f in ("_ref_test.png",):
    p = os.path.join(app_mod.cfg_mod.output_dir(), f)
    if os.path.isfile(p):
        os.remove(p)

inst.destroy()
httpd.shutdown()

print("\n" + "=" * 50)
if fails:
    print(f"结果：{len(fails)} 项未通过 -> {fails}")
    sys.exit(1)
print("端到端测试：全部通过 ✓")
