# -*- coding: utf-8 -*-
"""ComfyUI 功能测试：本地模拟一个 ComfyUI 服务（HTTP + WebSocket）跑通全流程。"""
import base64
import hashlib
import io
import json
import os
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PIL import Image  # noqa: E402

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------------------
# 模拟 ComfyUI
# ---------------------------------------------------------------------------

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
STATE = {"current": None}
SUBMITTED = []
UPLOADED = []
HISTORY = {}


def make_png(color, size=(256, 256)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


PNG_BYTES = make_png((120, 90, 250))


def ws_frame(text: str) -> bytes:
    payload = text.encode("utf-8")
    head = bytearray([0x81])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    return bytes(head) + payload


def finish_job(pid: str, delay: float = 1.0) -> None:
    def run():
        time.sleep(delay)
        HISTORY[pid] = {
            "prompt": [pid, 0, {}],
            "status": {"status_str": "success", "completed": True, "messages": []},
            "outputs": {
                "9": {"images": [
                    {"filename": f"{pid}_00001_.png", "subfolder": "", "type": "output"},
                ]},
            },
        }
    threading.Thread(target=run, daemon=True).start()


class ComfyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/system_stats":
            self._json(200, {
                "system": {"comfyui_version": "0.3.27", "python_version": "3.11.9"},
                "devices": [{"name": "cuda:0", "vram_total": 8589934592}],
            })
        elif u.path == "/queue":
            self._json(200, {"queue_running": [], "queue_pending": []})
        elif u.path.startswith("/history/"):
            pid = u.path.rsplit("/", 1)[-1]
            self._json(200, {pid: HISTORY[pid]} if pid in HISTORY else {})
        elif u.path == "/view":
            q = parse_qs(u.query)
            if q.get("filename"):
                self._bytes(200, PNG_BYTES, "image/png")
            else:
                self._json(400, {"error": "no filename"})
        elif u.path == "/ws":
            self._ws()
        else:
            self._json(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if u.path == "/prompt":
            try:
                data = json.loads(raw)
            except ValueError:
                self._json(400, {"error": "bad json"})
                return
            SUBMITTED.append(data.get("prompt"))
            pid = f"pid-{len(SUBMITTED)}"
            STATE["current"] = pid
            finish_job(pid, delay=1.2)
            self._json(200, {"prompt_id": pid, "number": 1, "node_errors": {}})
        elif u.path == "/upload/image":
            UPLOADED.append(raw)
            self._json(200, {"name": "uploaded_ref.png", "subfolder": "", "type": "input"})
        else:
            self._json(404, {"error": "not found"})

    # ---- WebSocket ----
    def _ws(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._json(400, {"error": "no key"})
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        sock = self.connection
        try:
            # 等客户端提交任务
            for _ in range(80):
                if STATE.get("current"):
                    break
                time.sleep(0.05)
            pid = STATE.get("current")
            for i in range(1, 6):
                sock.sendall(ws_frame(json.dumps(
                    {"type": "progress", "data": {"value": i, "max": 5, "prompt_id": pid}})))
                time.sleep(0.2)
            sock.sendall(ws_frame(json.dumps(
                {"type": "executing", "data": {"node": None, "prompt_id": pid}})))
            time.sleep(0.5)
        except Exception:  # noqa: BLE001
            pass


with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
httpd = ThreadingHTTPServer(("127.0.0.1", PORT), ComfyHandler)
httpd.daemon_threads = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"
print(f"模拟 ComfyUI 已启动: {BASE}\n")

# ---------------------------------------------------------------------------

from api_image_studio.core.comfy_client import ComfyUIClient  # noqa: E402
from api_image_studio.core import config as cfg_mod  # noqa: E402
from api_image_studio.core import history as hist_mod  # noqa: E402

WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {
        "seed": 12345, "steps": 20, "cfg": 7.0, "sampler_name": "euler",
        "scheduler": "normal", "denoise": 1.0,
        "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
        "latent_image": ["5", 0]}, "_meta": {"title": "KSampler"}},
    "4": {"class_type": "CheckpointLoaderSimple",
          "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"},
          "_meta": {"title": "Load Checkpoint"}},
    "5": {"class_type": "EmptyLatentImage",
          "inputs": {"width": 512, "height": 512, "batch_size": 1},
          "_meta": {"title": "Empty Latent Image"}},
    "6": {"class_type": "CLIPTextEncode",
          "inputs": {"text": "a cat", "clip": ["4", 1]},
          "_meta": {"title": "CLIP Text Encode (Prompt)"}},
    "7": {"class_type": "CLIPTextEncode",
          "inputs": {"text": "blurry", "clip": ["4", 1]},
          "_meta": {"title": "CLIP Text Encode (Negative)"}},
    "8": {"class_type": "VAEDecode",
          "inputs": {"samples": ["3", 0], "vae": ["4", 2]}, "_meta": {"title": "VAE Decode"}},
    "9": {"class_type": "SaveImage",
          "inputs": {"filename_prefix": "ComfyUI", "images": ["8", 0]},
          "_meta": {"title": "Save Image"}},
    "10": {"class_type": "LoadImage",
           "inputs": {"image": "example.png", "upload": "image"},
           "_meta": {"title": "Load Image"}},
}

wf_path = os.path.join(cfg_mod.OUTPUT_DIR if not os.path.isdir(cfg_mod.output_dir())
                       else cfg_mod.output_dir(), "_test_workflow.json")
with open(wf_path, "w", encoding="utf-8") as f:
    json.dump(WORKFLOW, f, ensure_ascii=False)

print("== 1. 工作流解析 ==")
wf = ComfyUIClient.load_workflow(wf_path)
check("读取工作流", len(wf) == 8, f"{len(wf)} 个节点")
a = ComfyUIClient.analyze(wf)
check("识别正向提示词节点", (a.get("positive") or {}).get("node") == "6", str(a.get("positive")))
check("识别负向提示词节点", (a.get("negative") or {}).get("node") == "7", str(a.get("negative")))
check("识别尺寸节点", (a.get("size") or {}).get("node") == "5", str(a.get("size")))
check("识别种子节点", (a.get("seed") or {}).get("node") == "3", str(a.get("seed")))
check("识别批量节点", (a.get("batch") or {}).get("node") == "5", str(a.get("batch")))
check("识别参考图节点", len(a.get("image") or []) == 1 and a["image"][0]["node"] == "10")
check("识别输出节点", "9" in (a.get("outputs") or []), str(a.get("outputs")))
check("文本候选数量", len(a.get("text_candidates") or []) == 2, str(len(a.get("text_candidates") or [])))

print("\n== 2. 非 API 格式会被拒绝 ==")
bad = os.path.join(os.path.dirname(wf_path), "_bad_workflow.json")
with open(bad, "w", encoding="utf-8") as f:
    json.dump({"nodes": [{"id": 1}]}, f)
try:
    ComfyUIClient.load_workflow(bad)
    check("拒绝普通导出格式", False)
except ValueError as exc:
    check("拒绝普通导出格式", "API" in str(exc), str(exc)[:40])

print("\n== 3. 连接测试 ==")
client = ComfyUIClient({"comfy_url": BASE, "comfy_timeout": 60})
res = client.test_connection()
check("测试连接成功", res.get("ok") is True, res.get("message", "")[:60])
check("返回版本信息", "0.3.27" in res.get("message", ""), res.get("message", ""))

print("\n== 4. 生图（含 WebSocket 进度）==")
progress_events = []
out_dir = os.path.join(cfg_mod.ROOT_DIR, "output")
os.makedirs(out_dir, exist_ok=True)
result = client.generate(
    prompt="一只戴护目镜的橘猫",
    negative_prompt="模糊, 水印",
    width=768, height=1024, n=2, seed=42,
    workflow=wf, mapping={
        "positive_node": "6", "positive_input": "text",
        "negative_node": "7", "negative_input": "text",
        "size_node": "5", "width_field": "width", "height_field": "height",
        "seed_node": "3", "seed_field": "seed",
        "batch_node": "5", "batch_field": "batch_size",
    },
    save_dir=out_dir,
    progress=lambda d, t, m: progress_events.append((d, t, m)),
)
check("生图成功", result.get("ok") is True, str(result.get("error"))[:120])
check("取回 1 张图片", len(result.get("images") or []) == 1)
if result.get("images"):
    p = result["images"][0]["path"]
    check("图片已落盘", os.path.isfile(p), os.path.basename(p))
    check("图片内容有效", os.path.getsize(p) > 100)
    try:
        os.remove(p)
    except OSError:
        pass
sent = SUBMITTED[-1] if SUBMITTED else {}
check("提示词已注入工作流", sent.get("6", {}).get("inputs", {}).get("text") == "一只戴护目镜的橘猫",
      str(sent.get("6", {}).get("inputs", {}).get("text"))[:30])
check("负向提示词已注入", sent.get("7", {}).get("inputs", {}).get("text") == "模糊, 水印")
check("尺寸已注入", sent.get("5", {}).get("inputs", {}).get("width") == 768,
      str(sent.get("5", {}).get("inputs")))
check("批量已注入", sent.get("5", {}).get("inputs", {}).get("batch_size") == 2)
check("种子已注入", sent.get("3", {}).get("inputs", {}).get("seed") == 42)
check("原工作流未被污染", WORKFLOW["6"]["inputs"]["text"] == "a cat")
ws_progress = [e for e in progress_events if e[2].startswith("采样中")]
check("收到 WebSocket 进度", len(ws_progress) >= 3, f"{len(ws_progress)} 条")

print("\n== 5. 参考图上传 ==")
ref = os.path.join(out_dir, "_ref.png")
Image.new("RGB", (64, 64), (200, 120, 40)).save(ref)
r2 = client.generate(
    prompt="参考这张图", negative_prompt="", width=512, height=512, n=1, seed=1,
    workflow=wf, mapping={
        "positive_node": "6", "positive_input": "text",
        "size_node": "5", "width_field": "width", "height_field": "height",
        "image_node": "10", "image_field": "image",
    },
    save_dir=out_dir, reference_image=ref,
)
check("带参考图生成成功", r2.get("ok") is True, str(r2.get("error"))[:100])
check("参考图已上传", len(UPLOADED) >= 1)
check("文件名已写入 LoadImage",
      (SUBMITTED[-1].get("10", {}).get("inputs", {}).get("image") == "uploaded_ref.png"),
      str(SUBMITTED[-1].get("10", {}).get("inputs", {})))
if r2.get("images"):
    try:
        os.remove(r2["images"][0]["path"])
    except OSError:
        pass
os.remove(ref)

print("\n== 6. 界面集成 ==")
from api_image_studio import gui as app_mod  # noqa: E402

inst = app_mod.App()
inst.geometry("1440x930+20+10")
inst.update()

check("引擎默认值合法", inst.engine in ("api", "comfyui"), inst.engine)
inst._set_engine("comfyui", silent=True)
inst.update()
check("切到 ComfyUI 后显示对应面板", inst.frame_comfy.winfo_ismapped() == 1)
check("API 面板已隐藏", inst.frame_api.winfo_ismapped() == 0)

inst.ent_comfy_url.delete(0, "end")
inst.ent_comfy_url.insert(0, BASE)
inst._load_workflow_file(wf_path, notify=False)
inst.update()
check("工作流已载入界面", bool(inst._workflow))
check("映射下拉已启用", str(inst.map_menus["positive"].cget("state")) == "normal")
check("正向节点自动选中", inst._comfy_mapping.get("positive_node") == "6",
      str(inst._comfy_mapping.get("positive_node")))
check("尺寸节点自动选中", inst._comfy_mapping.get("size_node") == "5")
check("种子节点自动选中", inst._comfy_mapping.get("seed_node") == "3")

inst._test_comfy()
end = time.time() + 4
while time.time() < end:
    inst.update()
    time.sleep(0.05)
check("界面测试连接成功", inst.lbl_comfy_state.cget("text").startswith("✓"),
      inst.lbl_comfy_state.cget("text")[:50])

inst.txt_prompt.delete("1.0", "end")
inst.txt_prompt.insert("1.0", "界面发起的 ComfyUI 生成")
inst.ent_n.delete(0, "end")
inst.ent_n.insert(0, "1")
before = len(hist_mod.load_history())
inst.on_generate()
end = time.time() + 15
while time.time() < end:
    inst.update()
    time.sleep(0.05)
    if not inst.generating and inst.last_images:
        break
cards = [w for w in inst.gallery.winfo_children() if w is not inst.lbl_empty]
check("界面生成结束", inst.generating is False)
check("结果卡片出现", len(cards) >= 1, f"{len(cards)} 张")
check("历史记录 +1", len(hist_mod.load_history()) == before + 1)
check("界面请求已注入提示词",
      SUBMITTED[-1].get("6", {}).get("inputs", {}).get("text") == "界面发起的 ComfyUI 生成")

# 切回 API 引擎，确认面板还原
inst._set_engine("api", silent=True)
inst.update()
check("切回 API 面板", inst.frame_api.winfo_ismapped() == 1 and inst.frame_comfy.winfo_ismapped() == 0)
inst.destroy()

# 清理
hist_mod.clear_history()
for f in ("_test_workflow.json", "_bad_workflow.json"):
    p = os.path.join(out_dir, f)
    if os.path.isfile(p):
        os.remove(p)
httpd.shutdown()

print("\n" + "=" * 52)
if fails:
    print(f"结果：{len(fails)} 项未通过 -> {fails}")
    sys.exit(1)
print("ComfyUI 测试：全部通过 ✓")
