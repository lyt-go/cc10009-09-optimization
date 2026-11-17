"""失败URL规范化与原因码优先级。

重试队列的唯一定位键是「作业编号 + 作业版本 + 规范化URL」，所以规范化必须稳定、
幂等，并且与作业上配置的「URL去重策略」保持一致。这里的实现只依赖标准库。

规范化规则：
- 去掉首尾空白与 URL 片段（#fragment）
- scheme / host 统一小写，去掉默认端口
- 合并重复斜杠、处理 ``.`` / ``..``，去掉非根路径的结尾斜杠
- 查询参数按参数名、参数值排序，默认剥离跟踪参数（utm_*、gclid 等）
- 非保留字符做百分号解码后再统一编码，保证同一页面只产生一个规范化URL

去重策略里如果显式写了「保留跟踪参数」或「保留结尾斜杠」，则对应步骤保持原样，
避免把业务上确实不同的页面合并掉。
"""
from __future__ import annotations

import posixpath
import re
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}
TRACKING_KEYS = {
    "gclid",
    "fbclid",
    "spm",
    "ref",
    "referrer",
    "referer",
    "_ga",
    "_gl",
    "yclid",
    "msclkid",
}
TRACKING_PREFIXES = ("utm_",)
# 百分号编码时保留的字符：路径分隔符、查询分隔符和常见合法字符。
SAFE_PATH = "/:@!$&'()*+,;=-._~%"
SAFE_QUERY = "/:@!$&'()*+,;=-._~%?"


def _strip_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_KEYS or lowered.startswith(TRACKING_PREFIXES)


def _strategy_flag(strategy: str | None, *keywords: str) -> bool:
    text = str(strategy or "")
    return any(keyword in text for keyword in keywords)


def _normalize_path(path: str, *, keep_trailing_slash: bool) -> str:
    raw = path or "/"
    # 先合并重复斜杠，再消解 . 和 ..，避免 /a/../b 与 /b 被当成两个页面。
    raw = re.sub(r"/{2,}", "/", raw)
    if not raw.startswith("/"):
        raw = "/" + raw
    had_trailing_slash = raw.endswith("/") and raw != "/"
    decoded = unquote(raw)
    normalized = posixpath.normpath(decoded)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    # normpath 会吃掉结尾斜杠，保留策略下要按原样补回来。
    if keep_trailing_slash and had_trailing_slash and not normalized.endswith("/"):
        normalized += "/"
    if not keep_trailing_slash and normalized != "/":
        normalized = normalized.rstrip("/") or "/"
    return quote(normalized, safe=SAFE_PATH.replace("%", ""))


def _normalize_query(query: str, *, keep_tracking: bool) -> str:
    if not query:
        return ""
    pairs: list[tuple[str, str]] = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        key = unquote(key)
        value = unquote(value)
        if not keep_tracking and _strip_tracking(key):
            continue
        pairs.append((key, value))
    # 同一参数名出现多次时同样排序，保证 ?a=2&a=1 与 ?a=1&a=2 等价。
    pairs.sort(key=lambda item: (item[0], item[1]))
    return "&".join(
        f"{quote(key, safe=SAFE_QUERY)}={quote(value, safe=SAFE_QUERY)}" if value
        else quote(key, safe=SAFE_QUERY)
        for key, value in pairs
    )


def normalize_url(raw_url: object, strategy: str | None = None) -> str:
    """返回用于去重和回写的规范化URL；空值返回空字符串。"""
    text = str(raw_url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        # 失败URL经常只保存了站点相对路径或省略 scheme 的地址，补默认 scheme 后再规范化。
        text = "http://" + text.lstrip("/") if text.startswith("//") else "http://" + text
    parts = urlsplit(text)
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return ""
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"
    if port and port != DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    if parts.username:
        auth = parts.username
        if parts.password:
            auth = f"{auth}:{parts.password}"
        netloc = f"{auth}@{netloc}"

    path = _normalize_path(
        parts.path,
        keep_trailing_slash=_strategy_flag(strategy, "保留结尾斜杠", "保留尾斜杠"),
    )
    query = _normalize_query(
        parts.query,
        keep_tracking=_strategy_flag(strategy, "保留跟踪参数", "保留查询参数"),
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def url_host(raw_url: object) -> str:
    """取小写主机名，用于起始URL的作业归属校验。"""
    text = str(raw_url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return (urlsplit(text).hostname or "").rstrip(".").lower()


def same_site(url: object, start_url: object) -> bool:
    """失败URL是否仍然属于起始URL所在站点（允许子域名）。"""
    host = url_host(url)
    start_host = url_host(start_url)
    if not host or not start_host:
        return False
    return host == start_host or host.endswith("." + start_host)


# 原因码优先级：数字越大越优先保留。合并同一URL的多个失败原因时，只把最高优先级
# 的原因作为主原因，其余原因放进「失败原因码列表」，冲突处置也沿用这套优先级。
REASON_PRIORITY = {
    "ROBOTS_DENIED": 90,
    "PARSE_ERROR": 80,
    "DNS_FAILURE": 70,
    "NETWORK_ERROR": 65,
    "TIMEOUT": 60,
    "HTTP_503": 55,
    "HTTP_502": 50,
    "HTTP_500": 45,
    "HTTP_429": 40,
    "HTTP_403": 30,
    "HTTP_404": 20,
}


def reason_priority(code: object) -> int:
    return REASON_PRIORITY.get(str(code or "").strip().upper(), 10)


def merge_reasons(existing: object, incoming: object) -> list[str]:
    """把已有原因码和新原因码按优先级合并，保持去重和稳定顺序。"""
    values: list[str] = []

    def _add(value: object) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                _add(item)
            return
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)

    _add(existing)
    _add(incoming)
    values.sort(key=lambda item: (-reason_priority(item), item))
    return values


def primary_reason(*codes: object) -> str:
    merged = merge_reasons(codes, None)
    return merged[0] if merged else ""
