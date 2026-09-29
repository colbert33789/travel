"""共享 HTTP 工具: 标准库实现(无第三方依赖), 带超时/重试/JSONP 解析."""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request

logger = logging.getLogger("holiday_traffic.http")

USER_AGENT = "Mozilla/5.0 (holiday-traffic-forecast; research)"
_JSONP_RE = re.compile(r"^[^(]*\((.*)\)\s*;?\s*$", re.S)


class FetchError(RuntimeError):
    """外部数据源请求失败(网络/鉴权/业务错误码)."""


def get_text(url: str, params: dict | None = None, timeout: float = 12.0,
             retries: int = 2, backoff: float = 0.8) -> str:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001 - 统一包装为 FetchError
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise FetchError(f"请求失败 {url.split('?')[0]}: {last_err}")


def get_json(url: str, params: dict | None = None, **kw):
    text = get_text(url, params, **kw)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise FetchError(f"非法 JSON: {url.split('?')[0]}") from e


def parse_jsonp(text: str):
    m = _JSONP_RE.match(text.strip())
    if not m:
        raise FetchError("非法 JSONP 响应")
    return json.loads(m.group(1))


def get_jsonp(url: str, params: dict | None = None, **kw):
    return parse_jsonp(get_text(url, params, **kw))
