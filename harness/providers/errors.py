"""Human-readable formatting for provider / LLM API failures."""

from __future__ import annotations

import json
import re
from html import unescape

_HTML_RE = re.compile(r"(?is)<(html|body|head|title|!doctype)\b")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"[ \t]+\n|\n{3,}|[ \t]{2,}")


def format_api_error(exc: BaseException, *, max_len: int = 900) -> str:
    """Turn OpenAI/Anthropic/HTTP exceptions into a short, actionable message."""
    name = type(exc).__name__
    status = _status_code(exc)
    detail = _extract_detail(exc)
    detail = _clean_text(detail)
    if not detail:
        detail = name

    head = name if status is None else f"{name} (HTTP {status})"
    body = f"{head}: {detail}"
    hint = _hint(name, status, detail)
    if hint:
        body = f"{body}\n\n提示：{hint}"
    if len(body) > max_len:
        body = body[: max_len - 1] + "…"
    return body


def is_error_assistant_text(text: str) -> bool:
    raw = (text or "").lstrip()
    return raw.startswith("[Error]") or raw.startswith("API 错误")


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    return None


def _extract_detail(exc: BaseException) -> str:
    for attr in ("message", "user_message"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value.strip():
            parsed = _maybe_json_message(value)
            if parsed:
                return parsed
            return value.strip()

    body = getattr(exc, "body", None)
    parsed = _from_body(body)
    if parsed:
        return parsed

    raw = str(exc).strip()
    parsed = _maybe_json_message(raw)
    return parsed or raw


def _from_body(body) -> str:
    if body is None:
        return ""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            return ""
    if isinstance(body, str):
        return _maybe_json_message(body) or body.strip()
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            for key in ("message", "msg", "detail"):
                if isinstance(err.get(key), str) and err[key].strip():
                    return err[key].strip()
        for key in ("message", "msg", "detail"):
            if isinstance(body.get(key), str) and body[key].strip():
                return body[key].strip()
        try:
            return json.dumps(body, ensure_ascii=False)
        except Exception:
            return str(body)
    return str(body)


def _maybe_json_message(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    if raw[0] not in "{[":
        # Often: "Error code: 401 - {'error': {...}}"
        brace = raw.find("{")
        if brace >= 0:
            raw = raw[brace:]
        else:
            return ""
    try:
        data = json.loads(raw.replace("'", '"'))
    except Exception:
        try:
            import ast

            # Several SDKs embed Python-dict error bodies in exception strings.
            # literal_eval safely recovers those without evaluating arbitrary code.
            data = ast.literal_eval(raw)
        except Exception:
            return ""
    if isinstance(data, dict):
        return _from_body(data)
    return ""


def _clean_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    if _HTML_RE.search(raw):
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
        if title:
            raw = unescape(title.group(1)).strip() or "HTML error page from API gateway"
        else:
            raw = _TAG_RE.sub(" ", raw)
            raw = unescape(raw)
            raw = _WS_RE.sub("\n", raw).strip()
            if len(raw) > 200:
                raw = raw[:200] + "…"
            raw = raw or "HTML error page from API gateway"
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = _WS_RE.sub("\n", raw).strip()
    return raw


def _hint(name: str, status: int | None, detail: str) -> str:
    low = f"{name} {detail}".lower()
    if status == 401 or "authentication" in low or "invalid api key" in low or "unauthorized" in low:
        return "检查 .env 里的 API Key / 环境变量是否与当前模型提供商匹配。"
    if status == 403 or "permission" in low or "forbidden" in low:
        return "当前密钥可能没有该模型权限，或账号被限流/封禁。"
    if status == 404 or "not found" in low or "does not exist" in low:
        return "模型名或 Base URL 可能写错；用 /model 确认当前模型配置。"
    if status == 429 or "rate" in low or "quota" in low:
        return "请求过快或额度不足，稍后再试，或换一个模型。"
    if status in (500, 502, 503, 529) or "overloaded" in low or "bad gateway" in low:
        return "上游服务暂时不可用，可稍后重试或切换备用模型。"
    if "connection" in low or "timeout" in low or "connect" in low or "proxy" in low:
        # Detect local proxy dead-end (very common with Clash on 7890).
        import os
        proxy = (
            os.getenv("HTTPS_PROXY")
            or os.getenv("HTTP_PROXY")
            or os.getenv("ALL_PROXY")
            or os.getenv("https_proxy")
            or os.getenv("http_proxy")
            or ""
        )
        if proxy:
            return (
                f"当前走代理 {proxy}。若代理软件未开，API 会全部失败；"
                "可先启动代理，或临时取消 HTTP(S)_PROXY / ALL_PROXY 后重试。"
                "DeepSeek/通义等国内接口通常可直连。"
            )
        return "检查网络、代理和 Base URL 是否可达。"
    if "context" in low or "too long" in low or "maximum" in low:
        return "上下文过长；可开新会话，或等自动压缩后再试。"
    return ""
