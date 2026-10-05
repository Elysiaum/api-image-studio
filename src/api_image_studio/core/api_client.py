# -*- coding: utf-8 -*-
"""通用生图 API 客户端（OpenAI 兼容协议）。

设计目标：自由接入任意中转站。
- 只需要 3 个信息：Base URL、API Key、模型名
- 自动识别 Base URL 是否已包含 /v1
- 文生图走 /images/generations，图生图默认走 /images/edits（多部分表单）
- 同时支持返回 url 与 b64_json 两种格式
- 对中转站常见的"参数不支持"报错做自动降级重试
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"/v\d+(?:beta)?/?$", re.IGNORECASE)


def build_endpoint(base_url: str, path: str) -> str:
    """把用户填写的 Base URL 与接口路径拼成完整 URL。

    支持这些写法：
      https://api.openai.com            -> https://api.openai.com/v1/images/generations
      https://api.openai.com/v1         -> https://api.openai.com/v1/images/generations
      https://xx.com/v1/images/generations -> 原样使用
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("Base URL 不能为空")

    # 用户直接填了完整接口地址
    if "/images/" in base:
        return base

    if not _VERSION_RE.search(base):
        base = base + "/v1"

    return base + path


def _guess_ext(content_type: str, url: str, default: str = ".png") -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
    }
    if ct in mapping:
        return mapping[ct]
    if url:
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
            return ".jpg" if ext == ".jpeg" else ext
    return default


def _extract_error(resp: requests.Response) -> str:
    """从中转站返回中尽量提取可读的错误信息。"""
    try:
        data = resp.json()
    except ValueError:
        text = (resp.text or "").strip()
        return text[:800] if text else f"HTTP {resp.status_code}"

    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("msg") or json.dumps(err, ensure_ascii=False)
            code = err.get("code") or err.get("type")
            return f"{msg}" + (f"（{code}）" if code else "")
        if isinstance(err, str):
            return err
        for key in ("message", "msg", "detail", "error_msg"):
            if isinstance(data.get(key), str):
                return data[key]
    return json.dumps(data, ensure_ascii=False)[:800]


def _iter_data_items(payload: Any) -> List[Dict[str, Any]]:
    """兼容不同中转站的返回结构，抽出图片数组。"""
    if isinstance(payload, dict):
        for key in ("data", "images", "output", "result"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                if all(isinstance(x, dict) for x in value):
                    return value
        # 有些站直接返回单个对象
        if any(k in payload for k in ("b64_json", "url", "image")):
            return [payload]
    if isinstance(payload, list) and payload and all(isinstance(x, dict) for x in payload):
        return payload
    return []


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class ImageAPIClient:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}
        self.timeout = int(self.cfg.get("timeout") or 300)
        self.verify_ssl = bool(self.cfg.get("verify_ssl", True))
        self.proxies = None
        proxy = (self.cfg.get("proxy") or "").strip()
        if proxy:
            self.proxies = {"http": proxy, "https": proxy}

    # -- 内部：请求 --------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        key = (self.cfg.get("api_key") or "").strip()
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _extra_body(self) -> Dict[str, Any]:
        raw = (self.cfg.get("extra_body") or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            raise ValueError("附加请求体不是合法的 JSON，请检查设置")

    # -- 公开：连通性测试 --------------------------------------------------
    def test_connection(self) -> Dict[str, Any]:
        """轻量连通性测试：请求 /models，失败也不算致命。"""
        base = (self.cfg.get("base_url") or "").strip().rstrip("/")
        if not base:
            return {"ok": False, "message": "Base URL 不能为空"}
        url = base + ("/models" if _VERSION_RE.search(base) else "/v1/models")
        try:
            resp = requests.get(
                url, headers=self._headers(), timeout=20,
                proxies=self.proxies, verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            return {"ok": False, "message": f"网络请求失败：{exc}", "url": url}
        if resp.status_code == 200:
            return {"ok": True, "message": "连接成功，鉴权正常", "url": url}
        return {
            "ok": False,
            "message": f"HTTP {resp.status_code}：{_extract_error(resp)}",
            "url": url,
        }

    # -- 公开：主入口 ------------------------------------------------------
    def generate(
        self,
        prompt: str,
        *,
        negative_prompt: str = "",
        size: str = "1024x1024",
        n: int = 1,
        model: str = "",
        quality: str = "",
        style: str = "",
        seed: Any = "",
        response_format: str = "",
        reference_image: Optional[str] = None,
        img2img_mode: str = "edits",
        save_dir: str,
        progress=None,
    ) -> Dict[str, Any]:
        """生成图片。reference_image 非空时走图生图。"""
        prompt = (prompt or "").strip()
        if not prompt:
            return {"ok": False, "error": "提示词不能为空"}

        model = (model or self.cfg.get("model") or "").strip()
        if not model:
            return {"ok": False, "error": "请先在设置中填写模型名"}

        n = max(1, min(int(n or 1), 10))
        os.makedirs(save_dir, exist_ok=True)

        started = time.time()
        collected: List[Dict[str, Any]] = []
        warnings: List[str] = []

        def report(done: int, total: int, msg: str) -> None:
            if callable(progress):
                try:
                    progress(done, total, msg)
                except Exception:
                    pass

        # 先整体请求 n 张；若中转站不支持 n>1 再逐张补齐
        if reference_image:
            report(0, n, "正在上传参考图并生成…")
            resp = self._request_edit(
                prompt, negative_prompt, size, n, model, quality, seed,
                response_format, reference_image, img2img_mode,
            )
        else:
            report(0, n, "正在生成…")
            resp = self._request_generation(
                prompt, negative_prompt, size, n, model, quality, style, seed,
                response_format,
            )

        if not resp.get("ok"):
            # n>1 失败时降级为逐张请求
            if n > 1 and resp.get("status") in (400, 422):
                warnings.append("服务端不支持一次生成多张，已自动改为逐张生成")
                resp = {"ok": True, "data": [], "meta": {}}
                for i in range(n):
                    report(i, n, f"正在生成第 {i + 1}/{n} 张…")
                    one = (
                        self._request_edit(
                            prompt, negative_prompt, size, 1, model, quality, seed,
                            response_format, reference_image, img2img_mode,
                        )
                        if reference_image
                        else self._request_generation(
                            prompt, negative_prompt, size, 1, model, quality, style,
                            seed, response_format,
                        )
                    )
                    if one.get("ok"):
                        resp["data"].extend(one.get("data", []))
                        resp["meta"] = one.get("meta", {})
                    else:
                        if not resp["data"]:
                            return one
                        warnings.append(f"第 {i + 1} 张失败：{one.get('error')}")
            else:
                return resp

        items = resp.get("data") or []
        if not items:
            return {"ok": False, "error": "接口未返回任何图片数据", "detail": resp.get("raw")}

        stamp = time.strftime("%Y%m%d_%H%M%S")
        total = len(items)
        for idx, item in enumerate(items, start=1):
            report(idx - 1, total, f"正在保存第 {idx}/{total} 张…")
            saved = self._save_item(item, save_dir, stamp, idx)
            if saved:
                collected.append(saved)
            else:
                warnings.append(f"第 {idx} 张图片数据无法解析")

        if not collected:
            return {"ok": False, "error": "图片数据解析失败", "detail": items[:1]}

        report(total, total, "完成")
        return {
            "ok": True,
            "images": collected,
            "warnings": warnings,
            "meta": resp.get("meta", {}),
            "elapsed": round(time.time() - started, 2),
        }

    # -- 内部：文生图 ------------------------------------------------------
    def _request_generation(
        self, prompt, negative_prompt, size, n, model,
        quality, style, seed, response_format,
    ) -> Dict[str, Any]:
        endpoint = build_endpoint(self.cfg.get("base_url", ""), "/images/generations")
        body: Dict[str, Any] = {"model": model, "prompt": prompt, "n": n}
        if size:
            body["size"] = size
        if quality:
            body["quality"] = quality
        if style:
            body["style"] = style
        if seed not in ("", None):
            body["seed"] = int(seed)
        if response_format:
            body["response_format"] = response_format
        if negative_prompt:
            # 标准协议没有该字段，但部分中转站支持
            body["negative_prompt"] = negative_prompt
        body.update(self._extra_body())

        return self._post_json(endpoint, body, n)

    # -- 内部：图生图 ------------------------------------------------------
    def _request_edit(
        self, prompt, negative_prompt, size, n, model,
        quality, seed, response_format, reference_image, mode,
    ) -> Dict[str, Any]:
        if mode == "generations":
            # 部分中转站用 /images/generations + base64 image 字段实现图生图
            endpoint = build_endpoint(self.cfg.get("base_url", ""), "/images/generations")
            with open(reference_image, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            body: Dict[str, Any] = {"model": model, "prompt": prompt, "n": n, "image": b64}
            if size:
                body["size"] = size
            if quality:
                body["quality"] = quality
            if seed not in ("", None):
                body["seed"] = int(seed)
            if response_format:
                body["response_format"] = response_format
            if negative_prompt:
                body["negative_prompt"] = negative_prompt
            body.update(self._extra_body())
            return self._post_json(endpoint, body, n)

        # 默认：标准 /images/edits 多部分表单
        endpoint = build_endpoint(self.cfg.get("base_url", ""), "/images/edits")
        data: Dict[str, Any] = {"model": model, "prompt": prompt, "n": str(n)}
        if size:
            data["size"] = size
        if quality:
            data["quality"] = quality
        if seed not in ("", None):
            data["seed"] = str(int(seed))
        if response_format:
            data["response_format"] = response_format
        if negative_prompt:
            data["negative_prompt"] = negative_prompt
        for key, value in self._extra_body().items():
            data[key] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

        mime = mimetypes.guess_type(reference_image)[0] or "image/png"
        filename = os.path.basename(reference_image)
        try:
            with open(reference_image, "rb") as f:
                files = {"image": (filename, f.read(), mime)}
        except OSError as exc:
            return {"ok": False, "error": f"读取参考图失败：{exc}"}

        return self._post_json(endpoint, data, n, files=files)

    # -- 内部：发请求 + 参数降级重试 ---------------------------------------
    def _post_json(
        self, endpoint: str, body: Dict[str, Any], n: int,
        files: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        headers = self._headers()
        payload = dict(body)
        dropped: List[str] = []
        last_error = "未知错误"
        last_status = 0

        for _ in range(4):
            try:
                if files:
                    resp = requests.post(
                        endpoint, headers=headers, data=payload, files=files,
                        timeout=self.timeout, proxies=self.proxies, verify=self.verify_ssl,
                    )
                else:
                    resp = requests.post(
                        endpoint, headers=headers, json=payload,
                        timeout=self.timeout, proxies=self.proxies, verify=self.verify_ssl,
                    )
            except requests.exceptions.Timeout:
                return {
                    "ok": False, "status": 0,
                    "error": f"请求超时（{self.timeout}s）。可尝试增大超时时间，或该中转站响应较慢。",
                    "endpoint": endpoint,
                }
            except requests.RequestException as exc:
                return {"ok": False, "status": 0, "error": f"网络错误：{exc}", "endpoint": endpoint}

            last_status = resp.status_code
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    return {
                        "ok": False, "status": 200, "endpoint": endpoint,
                        "error": "接口返回的不是 JSON，可能是中转站地址填写有误（返回了网页）",
                        "raw": (resp.text or "")[:400],
                    }
                items = _iter_data_items(data)
                if not items:
                    return {
                        "ok": False, "status": 200, "endpoint": endpoint,
                        "error": "接口返回中找不到图片数据", "raw": data,
                    }
                return {
                    "ok": True, "status": 200, "data": items,
                    "meta": {"dropped_params": dropped, "endpoint": endpoint},
                }

            last_error = _extract_error(resp)
            last_status = resp.status_code

            # 401/403 直接返回，不做降级
            if resp.status_code in (401, 403):
                return {
                    "ok": False, "status": resp.status_code, "endpoint": endpoint,
                    "error": f"鉴权失败（HTTP {resp.status_code}）：{last_error}",
                }

            # 尝试从报错中找出不被支持的字段并移除后重试
            removed = self._drop_offending(payload, last_error, dropped)
            if not removed:
                break

        return {
            "ok": False, "status": last_status, "endpoint": endpoint,
            "error": f"HTTP {last_status}：{last_error}",
            "detail": {"dropped_params": dropped, "endpoint": endpoint},
        }

    @staticmethod
    def _drop_offending(payload: Dict[str, Any], message: str, dropped: List[str]) -> bool:
        """根据报错文本移除不受支持的字段。返回是否做了改动。"""
        msg = (message or "").lower()
        candidates = [
            "response_format", "quality", "style", "seed",
            "negative_prompt", "size", "n",
        ]
        changed = False
        for field in candidates:
            if field in payload and field in dropped:
                continue
            if field in msg and any(
                kw in msg for kw in
                ("unsupported", "not support", "unknown", "unrecognized",
                 "invalid", "unexpected", "不支持", "未知", "多余", "无法识别", "非法")
            ):
                payload.pop(field, None)
                dropped.append(field)
                changed = True
        return changed

    # -- 内部：保存图片 ----------------------------------------------------
    def _save_item(
        self, item: Dict[str, Any], save_dir: str, stamp: str, idx: int,
    ) -> Optional[Dict[str, Any]]:
        b64 = item.get("b64_json") or item.get("b64") or item.get("base64")
        url = item.get("url") or item.get("image_url")
        revised = item.get("revised_prompt") or ""

        if not b64 and not url:
            # 有些站把 base64 直接放在 image 字段（可能带 data uri 前缀）
            raw = item.get("image")
            if isinstance(raw, str):
                if raw.startswith("data:"):
                    b64 = raw.split(",", 1)[-1]
                elif len(raw) > 200 and not raw.startswith("http"):
                    b64 = raw
                elif raw.startswith("http"):
                    url = raw

        ext = ".png"
        try:
            if b64:
                if isinstance(b64, str) and b64.startswith("data:"):
                    header, _, b64 = b64.partition(",")
                    ext = _guess_ext(header.split(";")[0].replace("data:", ""), "")
                content = base64.b64decode(b64)
            elif url:
                r = requests.get(
                    url, timeout=120, proxies=self.proxies, verify=self.verify_ssl,
                )
                r.raise_for_status()
                content = r.content
                ext = _guess_ext(r.headers.get("Content-Type", ""), url)
            else:
                return None
        except Exception:
            return None

        filename = f"{stamp}_{idx}{ext}"
        path = os.path.join(save_dir, filename)
        # 避免重名
        dup = 1
        while os.path.exists(path):
            filename = f"{stamp}_{idx}_{dup}{ext}"
            path = os.path.join(save_dir, filename)
            dup += 1

        try:
            with open(path, "wb") as f:
                f.write(content)
        except OSError:
            return None

        return {
            "path": path,
            "filename": filename,
            "url": url or "",
            "revised_prompt": revised,
            "size_bytes": len(content),
        }
