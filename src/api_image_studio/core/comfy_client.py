# -*- coding: utf-8 -*-
"""ComfyUI 生图客户端。

ComfyUI 的 HTTP API 用法：
  1. POST /prompt       提交一份「API 格式」的工作流 JSON，拿到 prompt_id
  2. WS   /ws?clientId= 订阅执行进度（采样步数、当前节点）
  3. GET  /history/{id} 执行结束后取回输出，图片在 outputs[节点]["images"]
  4. GET  /view         下载图片
  5. POST /upload/image 上传参考图（工作流里含 LoadImage 节点时）

工作流由用户在 ComfyUI 里「导出（API格式）」得到，程序自动分析出
正向提示词 / 负向提示词 / 尺寸 / 种子 / 批量 / 参考图 对应的节点与字段。
"""
from __future__ import annotations

import copy
import json
import os
import random
import re
import threading
import time
import urllib.parse
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

# 采样器类节点（可能带 Advanced 后缀）
SAMPLER_RE = re.compile(r"ksampler", re.IGNORECASE)
TEXT_ENCODE_RE = re.compile(r"textencode|text_encode|cliptextencode", re.IGNORECASE)
LATENT_RE = re.compile(r"emptylatent|emptysd3latent|emptysdxllatent", re.IGNORECASE)
IMAGE_LOAD_RE = re.compile(r"^loadimage$|loadimage", re.IGNORECASE)
SAVE_RE = re.compile(r"saveimage|previewimage|saveanimated", re.IGNORECASE)

TEXT_INPUTS = ("text", "string", "value", "prompt", "positive", "negative", "text_g", "text_l")


def _as_ws_url(base: str, client_id: str) -> str:
    url = base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{url.rstrip('/')}/ws?clientId={client_id}"


def _title_of(node: Dict[str, Any]) -> str:
    meta = node.get("_meta") or {}
    return meta.get("title") or node.get("class_type") or ""


def _find_text_input(inputs: Dict[str, Any]) -> Optional[str]:
    """在一个节点的输入里找一个像是文本的字段。"""
    for key in TEXT_INPUTS:
        if key in inputs and isinstance(inputs[key], str):
            return key
    for key, val in inputs.items():
        if isinstance(val, str) and len(val) > 0:
            return key
    return None


class ComfyUIClient:
    def __init__(self, cfg: Dict[str, Any]):
        self.base = (cfg.get("comfy_url") or "http://127.0.0.1:8188").strip().rstrip("/")
        if not re.match(r"^https?://", self.base, re.IGNORECASE):
            self.base = "http://" + self.base
        self.timeout = int(cfg.get("comfy_timeout") or 600)
        self.client_id = uuid.uuid4().hex
        self._ws_error = ""

    # ------------------------------------------------------------------
    # 基础请求
    # ------------------------------------------------------------------
    def _url(self, path: str) -> str:
        return self.base + path

    def test_connection(self) -> Dict[str, Any]:
        """连通性测试：读 /system_stats。"""
        try:
            resp = requests.get(self._url("/system_stats"), timeout=15)
        except requests.RequestException as exc:
            return {
                "ok": False,
                "message": f"无法连接 ComfyUI（{self.base}）：{exc}",
            }
        if resp.status_code != 200:
            return {"ok": False, "message": f"HTTP {resp.status_code}，请确认地址是否正确"}
        try:
            data = resp.json()
        except ValueError:
            return {"ok": False, "message": "返回的不是 JSON，请确认这是 ComfyUI 服务"}

        info = ""
        system = data.get("system") or {}
        if system:
            info = f"{system.get('comfyui_version') or ''} {system.get('python_version') or ''}".strip()
        devices = data.get("devices") or []
        if devices:
            d = devices[0]
            info += f"  [{d.get('name', '')} {d.get('vram_total', 0) // 1048576}MB]"
        return {"ok": True, "message": f"ComfyUI 已连接 {info}".strip()}

    def queue_state(self) -> Dict[str, Any]:
        try:
            r = requests.get(self._url("/queue"), timeout=10)
            if r.status_code == 200:
                d = r.json()
                return {"running": len(d.get("queue_running") or []),
                        "pending": len(d.get("queue_pending") or [])}
        except Exception:  # noqa: BLE001
            pass
        return {}

    # ------------------------------------------------------------------
    # 工作流分析
    # ------------------------------------------------------------------
    @staticmethod
    def load_workflow(path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data:
            raise ValueError("工作流文件内容不是有效的节点字典")
        # 兼容「带 nodes 数组的完整导出」与「API 格式」两种结构
        if "nodes" in data and isinstance(data["nodes"], list):
            raise ValueError(
                "这看起来是普通导出格式。请在 ComfyUI 里用 "
                "「Workflow → Export (API)」导出 API 格式的工作流。"
            )
        for node_id, node in data.items():
            if not isinstance(node, dict) or "class_type" not in node:
                raise ValueError(f"节点 {node_id} 缺少 class_type，不是 API 格式工作流")
        return data

    @staticmethod
    def analyze(workflow: Dict[str, Any]) -> Dict[str, Any]:
        """分析工作流，找出可以注入参数的节点。"""
        nodes = workflow or {}
        result: Dict[str, Any] = {
            "text_candidates": [],   # [{node, input, title, text}]
            "positive": None,
            "negative": None,
            "size": None,            # {node, width, height, w, h}
            "seed": None,            # {node, input, value}
            "batch": None,           # {node, input, value}
            "image": [],             # [{node, input, value}]
            "outputs": [],           # [node_id]
            # 供下拉选择用的候选列表
            "size_candidates": [],
            "seed_candidates": [],
            "batch_candidates": [],
        }

        def inputs_of(node: Dict[str, Any]) -> Dict[str, Any]:
            return node.get("inputs") or {}

        # 1) 所有可能的文本节点
        for node_id, node in nodes.items():
            if not isinstance(node, dict):
                continue
            ctype = node.get("class_type") or ""
            ins = inputs_of(node)
            if TEXT_ENCODE_RE.search(ctype) or any(k in ins for k in ("text",)):
                field = _find_text_input(ins)
                if field is not None:
                    result["text_candidates"].append({
                        "node": str(node_id),
                        "input": field,
                        "title": _title_of(node),
                        "text": str(ins.get(field, ""))[:120],
                    })

        # 2) 顺着 KSampler 的 positive / negative 找到确切的节点
        for node_id, node in nodes.items():
            if not isinstance(node, dict):
                continue
            ctype = node.get("class_type") or ""
            ins = inputs_of(node)

            if SAMPLER_RE.search(ctype):
                for slot, key in (("positive", "positive"), ("negative", "negative")):
                    link = ins.get(key)
                    if isinstance(link, list) and link:
                        ref = str(link[0])
                        ref_node = nodes.get(ref) or nodes.get(str(ref))
                        if isinstance(ref_node, dict):
                            field = _find_text_input(inputs_of(ref_node))
                            if field:
                                result[slot] = {
                                    "node": ref, "input": field,
                                    "title": _title_of(ref_node),
                                    "text": str(inputs_of(ref_node).get(field, ""))[:120],
                                }
                for seed_key in ("seed", "noise_seed"):
                    if seed_key in ins and result["seed"] is None:
                        result["seed"] = {"node": str(node_id), "input": seed_key,
                                          "value": ins.get(seed_key)}
                    if seed_key in ins:
                        result["seed_candidates"].append({
                            "node": str(node_id), "input": seed_key,
                            "title": _title_of(node), "value": ins.get(seed_key),
                        })

            if LATENT_RE.search(ctype):
                if "width" in ins and "height" in ins:
                    cand = {
                        "node": str(node_id), "width": "width", "height": "height",
                        "w": ins.get("width"), "h": ins.get("height"),
                        "title": _title_of(node),
                    }
                    result["size_candidates"].append(cand)
                    if result["size"] is None:
                        result["size"] = cand
                if "batch_size" in ins:
                    cand = {"node": str(node_id), "input": "batch_size",
                            "title": _title_of(node), "value": ins.get("batch_size")}
                    result["batch_candidates"].append(cand)
                    if result["batch"] is None:
                        result["batch"] = cand

            if IMAGE_LOAD_RE.search(ctype) and "image" in ins:
                result["image"].append({
                    "node": str(node_id), "input": "image",
                    "title": _title_of(node), "value": ins.get("image"),
                })

            if SAVE_RE.search(ctype):
                result["outputs"].append(str(node_id))

        # 3) 兜底：没有 KSampler 时，用文本节点顺序猜
        cands = result["text_candidates"]
        if result["positive"] is None and cands:
            result["positive"] = {k: cands[0][k] for k in ("node", "input", "title", "text")}
        if result["negative"] is None and len(cands) > 1:
            result["negative"] = {k: cands[1][k] for k in ("node", "input", "title", "text")}

        return result

    # ------------------------------------------------------------------
    # 生图主流程
    # ------------------------------------------------------------------
    def generate(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        n: int,
        seed: Any,
        workflow: Dict[str, Any],
        mapping: Dict[str, Any],
        save_dir: str,
        reference_image: Optional[str] = None,
        progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        def report(done: int, total: int, msg: str) -> None:
            if callable(progress):
                try:
                    progress(done, total, msg)
                except Exception:  # noqa: BLE001
                    pass

        if not workflow:
            return {"ok": False, "error": "尚未导入 ComfyUI 工作流"}

        wf = copy.deepcopy(workflow)

        def set_input(node_id: Any, field: str, value: Any) -> bool:
            node = wf.get(str(node_id)) or wf.get(node_id)
            if not isinstance(node, dict):
                return False
            node.setdefault("inputs", {})
            node["inputs"][field] = value
            return True

        applied: List[str] = []

        if prompt and mapping.get("positive_node"):
            if set_input(mapping["positive_node"], mapping.get("positive_input") or "text", prompt):
                applied.append("提示词")
        if mapping.get("negative_node") and (negative_prompt or mapping.get("clear_negative")):
            if set_input(mapping["negative_node"], mapping.get("negative_input") or "text",
                         negative_prompt or ""):
                applied.append("负向提示词")
        if mapping.get("size_node"):
            if mapping.get("width_field"):
                set_input(mapping["size_node"], mapping["width_field"], int(width))
            if mapping.get("height_field"):
                set_input(mapping["size_node"], mapping["height_field"], int(height))
            applied.append("尺寸")
        if mapping.get("batch_node") and mapping.get("batch_field"):
            set_input(mapping["batch_node"], mapping["batch_field"], max(1, int(n)))
            applied.append("批量")
        if mapping.get("seed_node") and mapping.get("seed_field"):
            s = int(seed) if str(seed).strip() else random.randint(0, 2 ** 31 - 1)
            set_input(mapping["seed_node"], mapping["seed_field"], s)
            applied.append(f"种子={s}")

        warnings: List[str] = []

        # 参考图：先上传，再把文件名写进 LoadImage 节点
        if reference_image and mapping.get("image_node"):
            up = self.upload_image(reference_image)
            if up.get("ok"):
                if set_input(mapping["image_node"], mapping.get("image_field") or "image",
                             up["name"]):
                    applied.append("参考图")
            else:
                warnings.append(f"参考图上传失败：{up.get('error')}")

        # 先建立进度监听，再提交，避免漏掉开头的几条消息
        stop = threading.Event()
        ws_used = {"value": False}
        self._prompt_id_holder = {"id": None}
        listener = threading.Thread(
            target=self._listen_progress, args=(report, stop, ws_used), daemon=True
        )
        listener.start()
        time.sleep(0.15)

        # 提交
        report(0, 0, "正在提交任务…")
        try:
            resp = requests.post(
                self._url("/prompt"),
                json={"prompt": wf, "client_id": self.client_id},
                timeout=60,
            )
        except requests.RequestException as exc:
            stop.set()
            return {"ok": False, "error": f"提交失败：{exc}"}

        if resp.status_code != 200:
            stop.set()
            return {"ok": False, "error": f"提交失败 HTTP {resp.status_code}：{resp.text[:300]}"}

        try:
            payload = resp.json()
        except ValueError:
            stop.set()
            return {"ok": False, "error": "提交返回的不是 JSON"}

        if payload.get("node_errors"):
            stop.set()
            msgs = []
            for node_id, err in payload["node_errors"].items():
                detail = err.get("errors") or []
                first = detail[0] if detail else {}
                msgs.append(f"节点 {node_id}: {first.get('message') or first.get('details') or err}")
            return {"ok": False, "error": "工作流校验失败：\n" + "\n".join(msgs[:5])}
        if payload.get("error"):
            stop.set()
            return {"ok": False, "error": f"提交失败：{payload['error']}"}

        prompt_id = payload.get("prompt_id")
        if not prompt_id:
            stop.set()
            return {"ok": False, "error": "服务端未返回 prompt_id"}
        self._prompt_id_holder["id"] = prompt_id

        started = time.time()
        history_item = None
        last_note = 0.0
        while True:
            if time.time() - started > self.timeout:
                stop.set()
                return {"ok": False, "error": f"等待超时（{self.timeout}s），可在设置里调大超时时间"}
            try:
                h = requests.get(self._url(f"/history/{prompt_id}"), timeout=20)
                if h.status_code == 200:
                    data = h.json()
                    if prompt_id in data:
                        history_item = data[prompt_id]
                        break
            except requests.RequestException:
                pass
            now = time.time()
            if now - last_note > 1.2:
                last_note = now
                if not ws_used["value"]:
                    q = self.queue_state()
                    extra = ""
                    if q:
                        extra = f"（队列中 {q.get('pending', 0)} 个）"
                    report(0, 0, f"正在生成… {int(now - started)}s{extra}")
            time.sleep(0.4)

        stop.set()

        status = (history_item.get("status") or {})
        if status.get("status_str") == "error" or status.get("completed") is False:
            detail = ""
            for m in status.get("messages") or []:
                if isinstance(m, list) and len(m) > 1 and m[0] == "execution_error":
                    info = m[1] or {}
                    detail = f"{info.get('node_type')}: {info.get('exception_message')}"
                    break
            return {"ok": False, "error": f"ComfyUI 执行出错\n{detail}"}

        images = self._collect_images(history_item)
        if not images:
            return {"ok": False, "error": "执行完成但没有输出图片（工作流里需要有 SaveImage 节点）"}

        report(0, 0, "正在下载图片…")
        os.makedirs(save_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        saved: List[Dict[str, Any]] = []
        for i, item in enumerate(images, start=1):
            report(i - 1, len(images), f"正在保存第 {i}/{len(images)} 张…")
            got = self._download(item, save_dir, stamp, i)
            if got:
                saved.append(got)
            else:
                warnings.append(f"第 {i} 张下载失败：{item.get('filename')}")

        if not saved:
            return {"ok": False, "error": "图片下载失败"}

        report(len(images), len(images), "完成")
        return {
            "ok": True,
            "images": saved,
            "warnings": warnings,
            "elapsed": round(time.time() - started, 2),
            "applied": applied,
        }

    # ------------------------------------------------------------------
    # 进度
    # ------------------------------------------------------------------
    def _listen_progress(self, report, stop: threading.Event,
                         ws_used: Dict[str, bool]) -> None:
        try:
            import websocket  # websocket-client
        except ImportError:
            return
        try:
            ws = websocket.create_connection(
                _as_ws_url(self.base, self.client_id), timeout=8
            )
        except Exception as exc:  # noqa: BLE001
            self._ws_error = str(exc)
            return

        ws_used["value"] = True
        try:
            ws.settimeout(1.0)
            while not stop.is_set():
                try:
                    raw = ws.recv()
                except Exception:  # noqa: BLE001
                    continue
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                data = msg.get("data") or {}
                pid = data.get("prompt_id")
                current = (getattr(self, "_prompt_id_holder", None) or {}).get("id")
                if pid and current and pid != current:
                    continue
                mtype = msg.get("type")
                if mtype == "progress":
                    value = int(data.get("value") or 0)
                    total = int(data.get("max") or 0)
                    report(value, total, f"采样中 {value}/{total}")
                elif mtype == "executing":
                    node = data.get("node")
                    if node is None:
                        break
                    report(0, 0, f"正在执行节点 {node}…")
                elif mtype == "execution_error":
                    info = data or {}
                    report(0, 0, f"出错：{info.get('exception_message', '')}")
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 图片
    # ------------------------------------------------------------------
    @staticmethod
    def _collect_images(history_item: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for _node_id, node_out in (history_item.get("outputs") or {}).items():
            for img in (node_out or {}).get("images") or []:
                if isinstance(img, dict) and img.get("filename"):
                    out.append({
                        "filename": img.get("filename"),
                        "subfolder": img.get("subfolder") or "",
                        "type": img.get("type") or "output",
                    })
        return out

    def _download(self, item: Dict[str, Any], save_dir: str, stamp: str, idx: int):
        query = urllib.parse.urlencode({
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        try:
            r = requests.get(self._url(f"/view?{query}"), timeout=180)
            if r.status_code != 200:
                return None
            content = r.content
        except requests.RequestException:
            return None

        ext = os.path.splitext(item["filename"])[1] or ".png"
        name = f"{stamp}_{idx}{ext}"
        path = os.path.join(save_dir, name)
        dup = 1
        while os.path.exists(path):
            name = f"{stamp}_{idx}_{dup}{ext}"
            path = os.path.join(save_dir, name)
            dup += 1
        try:
            with open(path, "wb") as f:
                f.write(content)
        except OSError:
            return None
        return {
            "path": path,
            "filename": name,
            "url": "",
            "revised_prompt": "",
            "size_bytes": len(content),
            "source": item["filename"],
        }

    def upload_image(self, path: str) -> Dict[str, Any]:
        """上传参考图到 ComfyUI 的 input 目录。"""
        try:
            with open(path, "rb") as f:
                files = {"image": (os.path.basename(path), f.read(), "image/png")}
            r = requests.post(
                self._url("/upload/image"),
                files=files,
                data={"overwrite": "true", "type": "input"},
                timeout=120,
            )
        except (OSError, requests.RequestException) as exc:
            return {"ok": False, "error": str(exc)}
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        try:
            data = r.json()
        except ValueError:
            return {"ok": False, "error": "返回不是 JSON"}
        name = data.get("name")
        if not name:
            return {"ok": False, "error": "服务端未返回文件名"}
        sub = data.get("subfolder") or ""
        return {"ok": True, "name": f"{sub}/{name}" if sub else name}
