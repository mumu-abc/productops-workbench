#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一的 LLM 客户端（OpenAI 兼容协议）。

为什么要把三个模块的 API 调用收成一个文件：
    老的三个 demo 里各写了一遍 urllib 请求 + 重试 + 解析，三份代码、
    三套重试策略、三个错误处理分支。改一处忘两处是必然的。

真正的价值在于：**它必须能失败得体面**。
评测跑到第 87 条时网络断了，不应该抛异常把整轮结果丢掉 ——
所以这里所有失败都兜底成返回值（带 `ok=False`），由调用方决定怎么处理。
"""

import json
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from .config import Config

__all__ = ["is_real_mode", "chat", "chat_json", "LLMResult"]


class LLMResult:
    """统一返回结构。`ok=False` 时 content 里是可读的错误描述，不会抛异常。"""

    def __init__(self, ok: bool, content: str = "", tokens: int = 0,
                 latency: float = 0.0, error: str = ""):
        self.ok = ok
        self.content = content
        self.tokens = tokens
        self.latency = latency
        self.error = error

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LLMResult ok={self.ok} tokens={self.tokens} {self.error[:40]}>"


def is_real_mode(mode: Optional[str] = None) -> bool:
    mode = mode or Config.MODE
    return mode == "real" and bool(Config.API_KEY) and bool(Config.API_BASE)


def chat(messages: List[Dict[str, str]], temperature: float = 0.0,
         model: Optional[str] = None, timeout: Optional[int] = None) -> LLMResult:
    """
    调用 chat/completions。失败自动重试，重试耗尽后返回 ok=False 而不是抛异常。

    温度默认 0：评测裁判和分类器都必须稳定，
    同一份输入跑两次给出不同结论的评测系统没有公信力。
    """
    if not Config.API_KEY or not Config.API_BASE:
        return LLMResult(False, error="未配置 PO_API_KEY / PO_API_BASE")

    url = f"{Config.API_BASE}/chat/completions"
    payload = {
        "model": model or Config.MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    t0 = time.time()
    last_err = ""
    for attempt in range(max(1, Config.RETRIES)):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {Config.API_KEY}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or Config.TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            return LLMResult(
                True, content=content,
                tokens=int(usage.get("total_tokens", 0)) or len(content) // 2,
                latency=round(time.time() - t0, 2),
            )
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
                ValueError, TimeoutError, OSError) as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < Config.RETRIES - 1:
                time.sleep(1.2 * (attempt + 1))    # 线性退避

    return LLMResult(False, latency=round(time.time() - t0, 2), error=last_err)


def _extract_json_object(raw: str) -> Optional[dict]:
    """
    从模型输出里抠出第一个 JSON 对象。

    为什么必须容错：就算你在 prompt 里写「只输出 JSON，不要任何其他文字」，
    真实模型仍然经常回「好的，这是结果：```json {...}```」。
    只按 JSON 解析必然失败，正则抠一下能救回绝大多数情况。
    """
    if not raw:
        return None
    start = raw.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = raw.find("{", start + 1)
    return None


def chat_json(messages: List[Dict[str, str]], temperature: float = 0.0) -> Dict:
    """
    调用并解析成 dict。失败返回 `{"_ok": False, "_error": ...}`。

    注意返回里始终带 `_ok` 字段：调用方必须显式判断，
    而不是拿到一个 {} 然后误以为「模型给了空结果」。
    """
    res = chat(messages, temperature=temperature)
    if not res.ok:
        return {"_ok": False, "_error": res.error}
    obj = _extract_json_object(res.content)
    if obj is None:
        return {"_ok": False, "_error": f"无法从输出中解析 JSON：{res.content[:120]}"}
    obj["_ok"] = True
    obj["_tokens"] = res.tokens
    obj["_latency"] = res.latency
    return obj
