#!/usr/bin/env python3
import base64
import threading
from concurrent.futures import ThreadPoolExecutor
import cgi
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import shutil
import shlex
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


LEGACY_API_KEY = os.environ.get("WETYPE_API_KEY", "")
BIND = os.environ.get("WETYPE_API_BIND", "127.0.0.1")
PORT = int(os.environ.get("WETYPE_API_PORT", "18080"))
BASE_DIR = Path(os.environ.get("WETYPE_DATA_DIR", "./data"))
KEY_STORE = Path(os.environ.get("WETYPE_KEYS_FILE", str(BASE_DIR / "api_keys.json")))
CONSOLE_TOKEN_FILE = Path(os.environ.get("WETYPE_CONSOLE_TOKEN_FILE", str(BASE_DIR / "console_token")))
CONSOLE_ASSETS_DIR = Path(os.environ.get("WETYPE_CONSOLE_ASSETS_DIR", str(BASE_DIR / "fonts")))
NGINX_ACCESS_LOG = Path(os.environ.get("WETYPE_NGINX_ACCESS_LOG", "/var/log/nginx/access.log"))
MAX_BODY_BYTES = int(os.environ.get("WETYPE_API_MAX_BODY_BYTES", str(30 * 1024 * 1024)))
ASR_TIMEOUT_SECONDS = int(os.environ.get("WETYPE_ASR_TIMEOUT", "240"))
SEARCH_TIMEOUT_SECONDS = int(os.environ.get("WETYPE_SEARCH_TIMEOUT", "20"))
ASR_PYTHON = os.environ.get("WETYPE_ASR_PYTHON", "python3")
ASR_SCRIPT = os.environ.get("WETYPE_ASR_SCRIPT", str(BASE_DIR / "sensevoice_transcribe.py"))
DEFAULT_MODEL = "wetype-voice"
ASR_CONFIG = Path(os.environ.get("WETYPE_CONFIG", str(BASE_DIR / "config.json")))


def asr_config():
    return json.loads(ASR_CONFIG.read_text(encoding="utf-8"))



def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    return datetime.now().strftime("%Y-%m-%d")


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler, status, body):
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(encoded)


def static_response(handler, path, content_type):
    body = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
    handler.end_headers()
    handler.wfile.write(body)


def redirect(handler, location):
    handler.send_response(303)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def key_digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mask_key(value):
    if not value:
        return ""
    return f"{value[:7]}{'*' * 24}{value[-4:]}"


def generate_api_key():
    return "sk-" + secrets.token_urlsafe(30)


def load_store():
    store = read_json(KEY_STORE, {"keys": []})
    if "keys" not in store or not isinstance(store["keys"], list):
        store = {"keys": []}
    changed = False
    if LEGACY_API_KEY and not any(item.get("sha256") == key_digest(LEGACY_API_KEY) for item in store["keys"]):
        store["keys"].append(
            {
                "id": secrets.token_hex(8),
                "name": "legacy-env-key",
                "sha256": key_digest(LEGACY_API_KEY),
                "masked": mask_key(LEGACY_API_KEY),
                "scopes": ["asr", "search"],
                "created_at": now_iso(),
                "last_used_at": None,
                "enabled": True,
                "source": "env",
            }
        )
        changed = True
    if changed:
        write_json(KEY_STORE, store)
    return store


def save_store(store):
    write_json(KEY_STORE, store)


def list_keys():
    return load_store()["keys"]


KEY_LOCK = threading.RLock()


def find_key(supplied, scope=None, touch=False):
    with KEY_LOCK:
        return _find_key(supplied, scope, touch)


def _find_key(supplied, scope=None, touch=False):
    if not supplied:
        return None
    digest = key_digest(supplied)
    store = load_store()
    matched = None
    for item in store["keys"]:
        if item.get("enabled", True) and hmac.compare_digest(item.get("sha256", ""), digest):
            scopes = item.get("scopes", ["asr", "search"])
            if scope and scope not in scopes:
                return None
            matched = item
            break
    if matched and touch:
        matched["last_used_at"] = now_iso()
        usage = matched.setdefault("usage", {})
        usage["total"] = int(usage.get("total", 0)) + 1
        if scope in {"asr", "search"}:
            usage[scope] = int(usage.get(scope, 0)) + 1
        days = usage.setdefault("days", {})
        day = today()
        day_usage = days.setdefault(day, {"total": 0, "asr": 0, "search": 0})
        day_usage["total"] = int(day_usage.get("total", 0)) + 1
        if scope in {"asr", "search"}:
            day_usage[scope] = int(day_usage.get(scope, 0)) + 1
        save_store(store)
    return matched


def usage_summary():
    keys = list_keys()
    totals = {"total": 0, "asr": 0, "search": 0}
    rows = []
    for item in keys:
        usage = item.get("usage", {})
        total = int(usage.get("total", 0))
        asr = int(usage.get("asr", 0))
        search = int(usage.get("search", 0))
        totals["total"] += total
        totals["asr"] += asr
        totals["search"] += search
        rows.append(
            {
                "id": item.get("id"),
                "name": item.get("name", ""),
                "masked": item.get("masked", ""),
                "enabled": item.get("enabled", True),
                "last_used_at": item.get("last_used_at"),
                "usage": {"total": total, "asr": asr, "search": search},
            }
        )
    rows.sort(key=lambda row: row["usage"]["total"], reverse=True)
    return {"totals": totals, "keys": rows}


LOG_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] "(?P<request>[^"]*)" '
    r"(?P<status>\d{3}) (?P<size>\S+)(?: \"(?P<referer>[^\"]*)\" \"(?P<ua>[^\"]*)\")?"
)
STATIC_SUFFIXES = {
    ".css",
    ".js",
    ".mjs",
    ".map",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".txt",
    ".xml",
}
KNOWN_PAGE_PATHS = {"/", "/index.html", "/time", "/serial.html", "/blog-index.html", "/console", "/console-login"}


def parse_access_log_line(line):
    match = LOG_RE.match(line)
    if not match:
        return None
    request = match.group("request")
    try:
        parts = shlex.split(request)
    except ValueError:
        parts = request.split()
    if len(parts) < 2:
        return None
    try:
        timestamp = datetime.strptime(match.group("time"), "%d/%b/%Y:%H:%M:%S %z")
    except ValueError:
        return None
    return {
        "ip": match.group("ip"),
        "time": timestamp,
        "method": parts[0],
        "target": parts[1],
        "status": int(match.group("status")),
        "size": 0 if match.group("size") == "-" else int(match.group("size")),
        "referer": match.group("referer") or "",
        "ua": match.group("ua") or "",
    }


def normalize_path(target):
    path = urllib.parse.urlparse(target).path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def is_static_path(path):
    suffix = Path(path).suffix.lower()
    return suffix in STATIC_SUFFIXES or path.startswith("/fonts/") or path.startswith("/console-assets/")


def website_dashboard_summary():
    now = datetime.now().astimezone()
    today_date = now.date()
    stats = {
        "date": today_date.isoformat(),
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "source": str(NGINX_ACCESS_LOG),
        "readable": False,
        "totals": {
            "requests": 0,
            "page_views": 0,
            "visitors": 0,
            "api_calls": 0,
            "console_views": 0,
            "errors": 0,
        },
        "top_pages": [],
        "hourly": [{"hour": f"{hour:02d}:00", "views": 0, "api": 0, "errors": 0} for hour in range(24)],
        "recent": [],
    }
    try:
        lines = NGINX_ACCESS_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        stats["error"] = "access_log_not_found"
        return stats
    except PermissionError:
        stats["error"] = "access_log_permission_denied"
        return stats

    stats["readable"] = True
    visitors = set()
    page_counts = {}
    recent = []

    for line in lines:
        item = parse_access_log_line(line)
        if not item or item["time"].astimezone().date() != today_date:
            continue
        path = normalize_path(item["target"])
        is_api = path.startswith("/api/")
        is_console = path == "/console" or path.startswith("/console-login") or path.startswith("/console-api/")
        is_static = is_static_path(path)
        is_page = (
            item["method"] == "GET"
            and item["status"] < 400
            and path in KNOWN_PAGE_PATHS
            and not is_static
            and not path.startswith("/.")
            and not path.startswith("/api/")
            and not path.startswith("/console-api/")
        )

        stats["totals"]["requests"] += 1
        visitors.add(item["ip"])
        hour = item["time"].astimezone().hour
        if is_api:
            stats["totals"]["api_calls"] += 1
            stats["hourly"][hour]["api"] += 1
        if is_console:
            stats["totals"]["console_views"] += 1
        if item["status"] >= 400:
            stats["totals"]["errors"] += 1
            stats["hourly"][hour]["errors"] += 1
        if is_page:
            stats["totals"]["page_views"] += 1
            stats["hourly"][hour]["views"] += 1
            page_counts[path] = page_counts.get(path, 0) + 1
        if not is_static:
            recent.append(
                {
                    "time": item["time"].astimezone().strftime("%H:%M:%S"),
                    "ip": item["ip"],
                    "method": item["method"],
                    "path": path,
                    "status": item["status"],
                    "ua": item["ua"][:90],
                }
            )

    stats["totals"]["visitors"] = len(visitors)
    stats["top_pages"] = [
        {"path": path, "views": views}
        for path, views in sorted(page_counts.items(), key=lambda row: row[1], reverse=True)[:8]
    ]
    stats["recent"] = recent[-12:][::-1]
    return stats


def get_console_token():
    if CONSOLE_TOKEN_FILE.exists():
        token = CONSOLE_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    CONSOLE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONSOLE_TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
    os.chmod(CONSOLE_TOKEN_FILE, 0o600)
    return token


def boolish(value):
    return str(value).lower() in {"1", "true", "yes", "basic", "advanced"}


def safe_int(value, default, minimum=1, maximum=20):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def strip_html(text):
    return html.unescape(text or "").replace("\r", " ").replace("\n", " ").strip()


def bing_rss_search(query, max_results):
    params = urllib.parse.urlencode({"q": query, "format": "rss"})
    url = f"https://www.bing.com/search?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; wetype-asr-gateway/1.0)",
            "Accept": "application/rss+xml, application/xml, text/xml",
        },
    )
    with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
        xml_text = response.read(1024 * 1024)

    root = ET.fromstring(xml_text)
    results = []
    for index, item in enumerate(root.findall("./channel/item")):
        if len(results) >= max_results:
            break
        title = strip_html(item.findtext("title"))
        link = strip_html(item.findtext("link"))
        content = strip_html(item.findtext("description"))
        published = strip_html(item.findtext("pubDate"))
        results.append(
            {
                "title": title,
                "url": link,
                "content": content,
                "score": round(max(0.0, 1.0 - index * 0.05), 4),
                "published_date": published or None,
            }
        )
    return results


def tavily_like_search(data):
    query = str(data.get("query", "")).strip()
    if not query:
        raise ValueError("missing_query")
    max_results = safe_int(data.get("max_results", data.get("limit", 5)), 5, 1, 20)
    include_answer = boolish(data.get("include_answer", False))
    include_raw_content = boolish(data.get("include_raw_content", False))
    include_images = boolish(data.get("include_images", False))

    started = time.monotonic()
    results = bing_rss_search(query, max_results)
    if not include_raw_content:
        for result in results:
            result.pop("raw_content", None)
    answer = None
    if include_answer and results:
        answer = " ".join(result["content"] for result in results[:3] if result.get("content")) or None
    return {
        "query": query,
        "follow_up_questions": None,
        "answer": answer,
        "images": [] if include_images else [],
        "results": results,
        "response_time": round(time.monotonic() - started, 3),
    }


def run_asr(audio_path, language="", prompt="", model=""):
    started = time.monotonic()
    if prompt:
        raise ValueError("wetype_does_not_support_prompt")
    if model not in ("", DEFAULT_MODEL, "iic/SenseVoiceSmall", "SenseVoiceSmall"):
        raise ValueError("unsupported_model")
    config = asr_config()
    boundary = "ASR" + secrets.token_hex(16)
    audio = Path(audio_path).read_bytes()
    if not audio or len(audio) > 25 * 1024 * 1024:
        raise ValueError("empty_or_oversize_audio")
    suffix = Path(audio_path).suffix
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{DEFAULT_MODEL}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio{suffix}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode() + audio + f'\r\n--{boundary}--\r\n'.encode()
    request = urllib.request.Request(config["upstream"] + "/v1/audio/transcriptions", data=body,
        headers={"Authorization": "Bearer " + config["api_key"],
                 "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(request, timeout=ASR_TIMEOUT_SECONDS) as response:
            result = json.load(response)
    except Exception:
        return {"ok": False, "error": "wetype_upstream_unavailable"}
    result.update(ok=True, model=DEFAULT_MODEL, provider="WeType / Windows VM")
    result.update(language="auto", duration=None, response_time=round(time.monotonic() - started, 3))
    if language:
        result.setdefault("adapter_notes", []).append("language is detected by WeType; language hints are not enforced")
    return result


CONTROL_CSRF_TOKEN = secrets.token_urlsafe(32)
HEALTH_CACHE = {}
HEALTH_LOCK = threading.Lock()
RESTART_LAST = {}


def http_probe(url):
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            return {"ok": response.status == 200, "status": response.status,
                    "ms": round((time.monotonic()-started)*1000)}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:250]}


def cached_probe(name, ttl, function):
    with HEALTH_LOCK:
        entry = HEALTH_CACHE.get(name)
        if entry and time.monotonic() - entry[0] < ttl:
            return entry[1]
        result = function()
        result["checked_at"] = now_iso()
        HEALTH_CACHE[name] = (time.monotonic(), result)
        return result


def wetype_status():
    def check():
        with ThreadPoolExecutor(max_workers=2) as pool:
            vm_future = pool.submit(http_probe, asr_config()["upstream"] + "/health")
            iat_future = pool.submit(http_probe, asr_config().get("iat_health_url", "http://127.0.0.1:8768/health"))
            vm, iat = vm_future.result(), iat_future.result()
        errors = [name + ": " + item.get("error", "HTTP " + str(item.get("status")))
                  for name, item in (("WeType VM", vm), ("讯飞格式适配器", iat)) if not item["ok"]]
        return {"ok": vm["ok"] and iat["ok"], "provider": "WeType / Windows VM", "model": DEFAULT_MODEL,
                "note": "自动检测进程接口；真实识别可用性以音频转写为准", "checks": {"vm": vm, "iat": iat},
                "errors": errors}
    return cached_probe("asr", 5, check)


def search_status():
    def check():
        started = time.monotonic()
        try:
            results = bing_rss_search("Ubuntu documentation", 1)
            return {"ok": True, "provider": "Bing RSS / Tavily 兼容", "errors": [],
                    "note": "实际搜索探测成功，结果 " + str(len(results)) + " 条；60 秒缓存", "ms": round((time.monotonic()-started)*1000)}
        except Exception as exc:
            return {"ok": False, "provider": "Bing RSS / Tavily 兼容", "errors": [type(exc).__name__ + ": " + str(exc)[:300]],
                    "note": "实际搜索探测失败；60 秒后重新检测"}
    return cached_probe("search", 60, check)


def backend_status():
    return {"asr": wetype_status(), "search": search_status(), "keys": {"count": len(list_keys())},
            "csrf_token": CONTROL_CSRF_TOKEN, "server_time": now_iso(), "restart_enabled": asr_config().get("enable_restart", False)}


CONSOLE_HTML = r"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>WeType ASR API 控制台</title>
    <style>
      :root { color-scheme: dark; --bg:#121314; --rail:#1b1c1e; --panel:#1f2022; --panel-2:#26272a; --text:#f4f4f5; --muted:#a1a1aa; --line:#34363a; --accent:#22c55e; --danger:#ef4444; }
      * { box-sizing: border-box; }
      body { margin:0; min-height:100vh; background:var(--bg); color:var(--text); font-family:MiSans,"Microsoft YaHei",SimHei,"PingFang SC","Noto Sans CJK SC",ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
      .layout { display:grid; grid-template-columns:234px 1fr; min-height:100vh; }
      aside { background:var(--rail); border-right:1px solid var(--line); padding:22px 16px; display:flex; flex-direction:column; }
      .brand { font-size:1.45rem; font-weight:800; margin-bottom:26px; }
      nav { display:grid; gap:8px; }
      .nav-item { display:flex; align-items:center; gap:10px; min-height:48px; padding:0 14px; color:var(--muted); border-radius:8px; font-weight:650; cursor:pointer; user-select:none; }
      .nav-item.active { border:1px solid #f4f4f5; color:#fff; background:#3a3a3d; }
      .dot { width:10px; height:10px; border-radius:50%; background:var(--accent); flex:0 0 auto; }
      .spacer { flex:1; }
      .foot { color:var(--muted); font-size:.82rem; line-height:1.65; display:grid; gap:8px; }
      .foot code { color:#d9f99d; word-break:break-all; font-family:"Cascadia Mono",Consolas,"SFMono-Regular","Liberation Mono",monospace; font-size:.92em; }
      main { padding:42px 54px 60px; }
      h1 { margin:0 0 26px; font-size:1.75rem; }
      .copy { max-width:940px; color:#e4e4e7; line-height:1.8; margin-bottom:24px; }
      .toolbar { display:flex; gap:12px; align-items:center; margin:24px 0; flex-wrap:wrap; }
      button,input,select { font:inherit; border-radius:8px; border:1px solid var(--line); background:#111; color:var(--text); min-height:38px; }
      input,select { padding:0 12px; }
      button { padding:0 14px; cursor:pointer; font-weight:650; }
      button.primary { background:#f4f4f5; color:#111; border-color:#f4f4f5; }
      button.danger { color:#fecaca; border-color:#7f1d1d; background:#2b1515; }
      table { width:100%; max-width:1080px; border-collapse:collapse; background:var(--panel); border-radius:8px; overflow:hidden; }
      th,td { text-align:left; padding:15px 14px; border-bottom:1px solid var(--line); vertical-align:middle; }
      th { color:#d4d4d8; font-size:.9rem; }
      td { color:#f4f4f5; font-weight:560; }
      tr:last-child td { border-bottom:0; }
      .badge { display:inline-flex; align-items:center; gap:6px; padding:4px 9px; border-radius:999px; background:#16351f; color:#bbf7d0; font-size:.78rem; }
      .badge.off { background:#3b1c1c; color:#fecaca; }
      .status-grid,.metric-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; max-width:1080px; margin:0 0 22px; }
      .status-card,.doc-card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; }
      .status-card strong,.doc-card strong { display:block; margin-bottom:8px; }
      .status-card span,.doc-card span { color:var(--muted); line-height:1.55; font-size:.92rem; }
      .status-card > span { display:block; margin-top:6px; }
      .status-card > button { display:block; margin-top:12px; }
      button:disabled { opacity:.55; cursor:wait; }
      .dashboard-grid { display:grid; grid-template-columns:minmax(0,1.15fr) minmax(320px,.85fr); gap:14px; max-width:1180px; align-items:start; }
      .metric-grid.dashboard-metrics { grid-template-columns:repeat(4,minmax(0,1fr)); max-width:1180px; }
      .metric-number { display:block; margin-top:8px; font-size:2rem; font-weight:800; letter-spacing:.01em; }
      .mini-note { color:var(--muted); font-size:.84rem; line-height:1.6; }
      .bar-chart { display:grid; grid-template-columns:repeat(24,minmax(8px,1fr)); align-items:end; gap:4px; height:150px; padding-top:12px; }
      .bar { min-height:2px; border-radius:4px 4px 0 0; background:linear-gradient(180deg,#86efac,#22c55e); opacity:.92; position:relative; }
      .bar.api { background:linear-gradient(180deg,#93c5fd,#3b82f6); }
      .bar.error { background:linear-gradient(180deg,#fca5a5,#ef4444); }
      .bar:hover::after { content:attr(data-label); position:absolute; left:50%; bottom:calc(100% + 8px); transform:translateX(-50%); white-space:nowrap; background:#09090b; border:1px solid var(--line); border-radius:6px; padding:5px 7px; color:#fff; font-size:.75rem; z-index:5; }
      .compact-table th,.compact-table td { padding:11px 12px; font-size:.9rem; }
      .path-cell { max-width:360px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .toast { display:none; max-width:1080px; margin:0 0 18px; padding:13px 14px; border-radius:8px; background:var(--panel-2); border:1px solid var(--line); white-space:pre-wrap; }
      .toast.show { display:block; }
      .view { display:none; }
      .view.active { display:block; }
      .doc-grid { display:grid; gap:14px; max-width:1080px; }
      pre { margin:10px 0 0; padding:14px; background:#0c0d0e; border:1px solid var(--line); border-radius:8px; overflow:auto; color:#d9f99d; line-height:1.65; font-family:"Cascadia Mono",Consolas,"SFMono-Regular","Liberation Mono",monospace; font-size:.92rem; }
      .modal { position:fixed; inset:0; display:none; align-items:center; justify-content:center; background:rgba(0,0,0,.66); }
      .modal.show { display:flex; }
      .dialog { width:min(520px,calc(100% - 32px)); background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:22px; }
      .dialog h2 { margin:0 0 18px; font-size:1.2rem; }
      .form-row { display:grid; gap:8px; margin-bottom:14px; }
      .checks { display:flex; gap:14px; color:var(--muted); }
      label { color:#e4e4e7; }
      code { color:#d9f99d; word-break:break-all; font-family:"Cascadia Mono",Consolas,"SFMono-Regular","Liberation Mono",monospace; font-size:.92em; }
      @media (max-width:1080px) { .dashboard-grid{grid-template-columns:1fr;} .metric-grid.dashboard-metrics{grid-template-columns:repeat(2,minmax(0,1fr));} }
      @media (max-width:820px) { .layout{grid-template-columns:1fr;} aside{display:none;} main{padding:28px 18px;} .status-grid,.metric-grid,.metric-grid.dashboard-metrics{grid-template-columns:1fr;} table{display:block; overflow-x:auto;} }
    </style>
  </head>
  <body>
    <div class="layout">
      <aside>
        <div class="brand">WeType ASR</div>
        <nav>
          <div class="nav-item active" data-view="dashboard"><span class="dot"></span>今日网站 Dashboard</div>
          <div class="nav-item" data-view="usage"><span class="dot"></span>用量信息</div>
          <div class="nav-item" data-view="keys"><span class="dot"></span>API keys</div>
          <div class="nav-item" data-view="asr"><span class="dot"></span>ASR</div>
          <div class="nav-item" data-view="search"><span class="dot"></span>Search</div>
          <div class="nav-item" data-view="docs"><span class="dot"></span>配置文档</div>
        </nav>
        <div class="spacer"></div>
        <div class="foot">
          <div>实际调用地址</div>
          <code>POST https://asr.example.com/api/asr</code>
          <code>GET/POST https://asr.example.com/api/search</code>
        </div>
      </aside>
      <main>
        <div class="toast" id="toast"></div>
        <section class="view active" id="view-dashboard">
          <h1>今日网站 Dashboard</h1>
          <p class="copy">从本机 Nginx access log 实时汇总今天的网站访问。静态资源不会计入页面访问，API 调用单独统计。</p>
          <div class="metric-grid dashboard-metrics" id="dashboardCards"></div>
          <div class="dashboard-grid">
            <div class="doc-card">
              <strong>24 小时访问走势</strong>
              <span id="dashboardNote">读取中...</span>
              <div class="bar-chart" id="dashboardChart"></div>
            </div>
            <div class="doc-card">
              <strong>热门页面</strong>
              <table class="compact-table"><thead><tr><th>路径</th><th>访问</th></tr></thead><tbody id="topPageRows"></tbody></table>
            </div>
          </div>
          <div class="doc-card" style="max-width:1180px;margin-top:14px">
            <strong>最近请求</strong>
            <table class="compact-table"><thead><tr><th>时间</th><th>IP</th><th>方法</th><th>路径</th><th>状态</th></tr></thead><tbody id="recentRows"></tbody></table>
          </div>
        </section>
        <section class="view" id="view-usage">
          <h1>用量信息</h1>
          <p class="copy">按 API key 统计调用次数。统计在每次鉴权成功后写入本机 key 存储。</p>
          <div class="metric-grid" id="usageCards"></div>
          <table><thead><tr><th>名称</th><th>Key</th><th>总调用</th><th>ASR</th><th>Search</th><th>最近使用</th><th>状态</th></tr></thead><tbody id="usageRows"></tbody></table>
        </section>
        <section class="view" id="view-keys">
          <h1>API keys</h1>
          <p class="copy">每个 key 可按权限调用 ASR 和 Search。Key 仅在创建时完整显示，请妥善保存。</p>
          <div class="status-grid" id="statusGrid"></div>
          <table>
            <thead><tr><th>名称</th><th>Key</th><th>权限</th><th>创建日期</th><th>最近使用日期</th><th>状态</th><th>操作</th></tr></thead>
            <tbody id="keyRows"></tbody>
          </table>
          <div class="toolbar"><button class="primary" id="createBtn">创建 API key</button><button id="refreshBtn">刷新状态</button></div>
        </section>
        <section class="view" id="view-asr">
          <h1>ASR</h1>
          <div class="doc-card" style="max-width:940px;margin-bottom:24px"><strong>本地接入配置</strong>
          <p>下方为本地服务凭据，不是讯飞云账户。默认隐藏密钥；仅在受保护的控制台内查看。</p>
          <div id="asrConfig">选择 ASR 页面后加载配置…</div>
          <button id="revealAsr" style="margin-top:14px">显示密钥</button>
          <p>OpenAI 格式使用专用 API key；原 /api/asr 仍使用「API keys」页中的 ASR key。讯飞格式使用 App ID / API Key / API Secret，单段最长 60 秒，仅返回最终结果，当前支持 raw PCM。密钥属于不同接入方式，请勿混用。</p></div>
          <p class="copy">ASR 已切换为 Windows 虚拟机中的微信输入法。原接口继续支持文件上传和 base64 音频，原有 ASR key 可继续使用。识别需要连接腾讯云，讯飞兼容接口在收到结束帧后返回全文。</p>
          <div class="doc-grid"><div class="doc-card"><strong>接口</strong><span><code>POST https://asr.example.com/api/asr</code></span><pre>Authorization: Bearer &lt;api-key&gt;
Content-Type: multipart/form-data
file=@audio.wav</pre></div></div>
        </section>
        <section class="view" id="view-search">
          <h1>Search</h1>
          <p class="copy">Search 返回 Tavily 兼容结构。当前后端为本机 Bing RSS wrapper，不是独立 SearXNG 服务。</p>
          <div class="doc-grid"><div class="doc-card"><strong>接口</strong><span><code>GET https://asr.example.com/api/search?query=...&amp;limit=5</code></span><pre>Authorization: Bearer &lt;api-key&gt;

POST /api/search
{"query":"cloudflare tunnel","max_results":5,"include_answer":true}</pre></div></div>
        </section>
        <section class="view" id="view-docs">
          <h1>配置文档</h1>
          <p class="copy">客户端只需要保存 API key，并通过 Authorization Bearer 或 X-API-Key 调用。</p>
          <div class="doc-grid">
            <div class="doc-card"><strong>鉴权</strong><pre>Authorization: Bearer &lt;api-key&gt;
# 或
X-API-Key: &lt;api-key&gt;</pre></div>
            <div class="doc-card"><strong>ASR 完整地址</strong><pre>POST https://asr.example.com/api/asr</pre></div>
            <div class="doc-card"><strong>Search 完整地址</strong><pre>GET  https://asr.example.com/api/search?query=关键词&amp;limit=5
POST https://asr.example.com/api/search</pre></div>
          </div>
        </section>
      </main>
    </div>
    <div class="modal" id="modal"><div class="dialog"><h2>创建 API key</h2><div class="form-row"><label for="keyName">名称</label><input id="keyName" placeholder="例如 Ubuntu / ClaudeCode / app-server" /></div><div class="form-row"><label>权限</label><div class="checks"><label><input type="checkbox" id="scopeAsr" checked /> ASR</label><label><input type="checkbox" id="scopeSearch" checked /> Search</label></div></div><div class="toolbar"><button class="primary" id="confirmCreate">创建</button><button id="cancelCreate">取消</button></div></div></div>
    <script>
      const $ = (id) => document.getElementById(id);
      const toast = $("toast");
      function showToast(text) { toast.textContent = text; toast.classList.add("show"); }
      async function api(path, options = {}) { const res = await fetch(path, { headers: { "Content-Type": "application/json", "X-Console-Action": controlCsrf }, ...options }); const text = await res.text(); const data = text ? JSON.parse(text) : {}; if (!res.ok) throw new Error(data.error || data.message || res.statusText); return data; }
      function dateOnly(value) { return value ? value.slice(0, 10) : "-"; }
      function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[ch])); }
      function setView(name) { document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === `view-${name}`)); document.querySelectorAll(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.view === name)); if (name === "dashboard") loadDashboard(); if (name === "usage") loadUsage(); if (name === "keys") load(); if (name === "asr") loadAsrConfig(); }
      document.querySelectorAll(".nav-item").forEach(item => item.addEventListener("click", () => setView(item.dataset.view)));
      function renderKeys(keys) { $("keyRows").innerHTML = keys.map((key) => `<tr><td>${escapeHtml(key.name)}</td><td><code>${escapeHtml(key.masked)}</code></td><td>${key.scopes.map(escapeHtml).join(", ")}</td><td>${dateOnly(key.created_at)}</td><td>${dateOnly(key.last_used_at)}</td><td><span class="badge ${key.enabled ? "" : "off"}">${key.enabled ? "启用" : "禁用"}</span></td><td><button data-toggle="${key.id}">${key.enabled ? "禁用" : "启用"}</button> <button class="danger" data-id="${key.id}">删除</button></td></tr>`).join(""); document.querySelectorAll("button[data-id]").forEach((button) => button.addEventListener("click", async () => { if (!confirm("删除后该 key 将无法继续调用 API。确认删除？")) return; await api(`/console-api/keys/${button.dataset.id}`, { method: "DELETE" }); await load(); })); document.querySelectorAll("button[data-toggle]").forEach((button) => button.addEventListener("click", async () => { await api(`/console-api/keys/${button.dataset.toggle}/toggle`, { method: "POST", body: "{}" }); await load(); })); }
      let controlCsrf = "";
      let controlBusy = false;
      let servicePending = {asr:0, search:0};
      function renderStatus(status) {
        controlCsrf = status.csrf_token;
        const card = (name, title) => {
          const s = status[name];
          if (s.ok && servicePending[name] && Date.now() > servicePending[name] - (name === "asr" ? 170000 : 10000)) servicePending[name] = 0;
          const pending = Date.now() < servicePending[name];
          const errorText = (s.errors || []).join("\n");
          return `<div class="status-card"><strong>${title}</strong><span style="color:${s.ok ? '#86efac' : '#fca5a5'}">${s.ok ? "健康" : "异常"} · ${escapeHtml(s.provider)}</span><span>${escapeHtml(s.note || "")}<br>检测时间：${escapeHtml(s.checked_at ? new Date(s.checked_at).toLocaleString("zh-CN",{hour12:false}) : "-")}</span>${errorText ? `<pre style="white-space:pre-wrap;word-break:break-word;color:#fca5a5;margin:10px 0">${escapeHtml(errorText)}</pre>` : ""}<button data-restart="${name}" ${pending || controlBusy || !status.restart_enabled ? "disabled" : ""}>${pending ? "已提交重启，正在等待恢复…" : (status.restart_enabled ? "重启 " + title : "重启未启用") }</button>${name === "asr" ? '<span>重启专用 VM，通常需 2～3 分钟；中断当前识别。</span>' : '<span>重启共用 API 网关，原 /api/asr 会短暂中断。</span>'}</div>`;
        };
        $("statusGrid").innerHTML = card("asr", "ASR") + card("search", "Search") + `<div class="status-card"><strong>Keys</strong><span>${status.keys.count} 个已登记 key<br>Authorization: Bearer 或 X-API-Key</span><span>此页每 15 秒刷新状态；Search 实际探测每 60 秒一次。</span><button id="checkServices">立即检测</button><span id="controlMessage" role="status"></span></div>`;
        document.querySelectorAll("button[data-restart]").forEach(button => button.addEventListener("click", () => restartService(button.dataset.restart)));
        $("checkServices").addEventListener("click", () => loadStatus(true));
      }
      async function loadStatus(force=false) {
        try { renderStatus(await api("/console-api/status" + (force ? "?refresh=1" : ""))); }
        catch (err) { showToast("状态检测失败：" + err.message); }
      }
      async function restartService(name) {
        const message = name === "asr" ? "重启 ASR 专用 Windows 虚拟机？会中断当前识别，通常需 2～3 分钟恢复。" : "重启 Search 所在的共用 API 网关？Search 和原 /api/asr 会短暂中断。";
        if (controlBusy || !confirm(message)) return;
        controlBusy = true;
        try {
          const result = await api("/console-api/control/restart", {method:"POST", headers:{"Content-Type":"application/json", "X-Console-Action":controlCsrf}, body:JSON.stringify({service:name})});
          servicePending[name] = Date.now() + (name === "asr" ? 180000 : 20000);
          showToast(result.message);
        } catch (err) { showToast("重启提交失败：" + err.message); }
        finally { controlBusy = false; loadStatus(); }
      }
      setInterval(() => { if (!document.hidden && $("view-keys").classList.contains("active")) loadStatus(); },15000);
      function renderUsage(data) { $("usageCards").innerHTML = `<div class="status-card"><strong>总调用</strong><span>${data.totals.total}</span></div><div class="status-card"><strong>ASR 调用</strong><span>${data.totals.asr}</span></div><div class="status-card"><strong>Search 调用</strong><span>${data.totals.search}</span></div>`; $("usageRows").innerHTML = data.keys.map(k => `<tr><td>${escapeHtml(k.name)}</td><td><code>${escapeHtml(k.masked)}</code></td><td>${k.usage.total}</td><td>${k.usage.asr}</td><td>${k.usage.search}</td><td>${dateOnly(k.last_used_at)}</td><td><span class="badge ${k.enabled ? "" : "off"}">${k.enabled ? "启用" : "禁用"}</span></td></tr>`).join(""); }
      function renderDashboard(data) { const totals = data.totals || {}; $("dashboardCards").innerHTML = `<div class="status-card"><strong>今日页面访问</strong><span class="metric-number">${totals.page_views || 0}</span><span>非静态 GET 页面</span></div><div class="status-card"><strong>今日访客 IP</strong><span class="metric-number">${totals.visitors || 0}</span><span>按访问 IP 去重</span></div><div class="status-card"><strong>API 调用</strong><span class="metric-number">${totals.api_calls || 0}</span><span>/api/* 请求</span></div><div class="status-card"><strong>错误请求</strong><span class="metric-number">${totals.errors || 0}</span><span>HTTP 4xx / 5xx</span></div>`; const max = Math.max(1, ...data.hourly.map(h => Math.max(h.views, h.api, h.errors))); $("dashboardChart").innerHTML = data.hourly.map(h => { const value = Math.max(h.views, h.api, h.errors); const cls = h.errors ? "error" : h.api > h.views ? "api" : ""; const height = Math.max(2, Math.round(value / max * 138)); return `<div class="bar ${cls}" style="height:${height}px" data-label="${escapeHtml(h.hour)} 页面 ${h.views} / API ${h.api} / 错误 ${h.errors}"></div>`; }).join(""); $("dashboardNote").textContent = data.readable ? `${data.generated_at} 更新 · 日志：${data.source}` : `日志不可读：${data.error || "unknown"}`; $("topPageRows").innerHTML = (data.top_pages.length ? data.top_pages : [{path:"暂无页面访问",views:"-"}]).map(row => `<tr><td class="path-cell"><code>${escapeHtml(row.path)}</code></td><td>${row.views}</td></tr>`).join(""); $("recentRows").innerHTML = (data.recent.length ? data.recent : [{time:"-",ip:"-",method:"-",path:"暂无请求",status:"-"}]).map(row => `<tr><td>${escapeHtml(row.time)}</td><td>${escapeHtml(row.ip)}</td><td>${escapeHtml(row.method)}</td><td class="path-cell"><code>${escapeHtml(row.path)}</code></td><td>${escapeHtml(row.status)}</td></tr>`).join(""); }
      let asrSecretsVisible = false;
      let asrSettings = null;
      function renderAsrConfig() {
        if (!asrSettings) return;
        const c = asrSettings;
        const entries = [["原接口", c.legacy_url], ["OpenAI Base URL", c.openai_base_url], ["模型", c.model], ["专用 API key", c.api_key], ["讯飞兼容 WebSocket", c.iat_url], ["App ID", c.iat.app_id], ["API Key", c.iat.api_key], ["API Secret", c.iat.api_secret]];
        $("asrConfig").innerHTML = entries.map(([name,value]) => {
          const secret = ["专用 API key", "API Key", "API Secret"].includes(name);
          return `<p><strong>${escapeHtml(name)}</strong><br><code>${escapeHtml(secret && !asrSecretsVisible ? "••••••••••••••••（已隐藏）" : value)}</code></p>`;
        }).join("");
        $("revealAsr").textContent = asrSecretsVisible ? "隐藏密钥" : "显示密钥";
      }
      async function loadAsrConfig() { try { asrSettings = await api("/console-api/asr-config"); renderAsrConfig(); } catch (err) { showToast(err.message); } }
      $("revealAsr").addEventListener("click", () => { asrSecretsVisible = !asrSecretsVisible; renderAsrConfig(); });
      async function load() { const data = await api("/console-api/keys"); renderKeys(data.keys); await loadStatus(); }
      async function loadUsage() { renderUsage(await api("/console-api/usage")); }
      async function loadDashboard() { renderDashboard(await api("/console-api/dashboard")); }
      $("createBtn").addEventListener("click", () => $("modal").classList.add("show"));
      $("cancelCreate").addEventListener("click", () => $("modal").classList.remove("show"));
      $("refreshBtn").addEventListener("click", load);
      $("confirmCreate").addEventListener("click", async () => { const scopes = []; if ($("scopeAsr").checked) scopes.push("asr"); if ($("scopeSearch").checked) scopes.push("search"); const result = await api("/console-api/keys", { method: "POST", body: JSON.stringify({ name: $("keyName").value || "unnamed", scopes }) }); $("modal").classList.remove("show"); $("keyName").value = ""; showToast(`请立即保存新 key，关闭后不能再次查看完整值：\n${result.key}`); await load(); });
      loadStatus();
      loadDashboard().catch((err) => showToast(err.message));
    </script>
  </body>
</html>"""
class Handler(BaseHTTPRequestHandler):
    server_version = "H9647123PublicAPI/2.0"

    def log_message(self, fmt, *args):
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), re.sub(r"(token=)[^ &]+", r"\1[redacted]", fmt % args)), flush=True)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, X-API-Key, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/console-assets/fonts/"):
            return self.handle_console_asset(parsed.path)
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path == "/console":
            return self.handle_console()
        if parsed.path == "/console-login-required":
            return html_response(
                self,
                401,
                "<!doctype html><html><head><meta charset=\"utf-8\"><title>Console login required</title></head>"
                "<body style=\"font-family:MiSans,Microsoft YaHei,SimHei,system-ui,sans-serif;margin:48px;background:#121314;color:#f4f4f5\">"
                "<h1>Console login required</h1><p>Use scripts/console_url.py to obtain your private console login URL.</p>"
                "</body></html>",
            )
        if parsed.path == "/console-login":
            return self.handle_login(parsed)
        if parsed.path == "/console-api/asr-config":
            if not self.require_console_auth():
                return
            config = asr_config()
            return json_response(self, 200, {key: value for key, value in config.items() if key != "upstream"})
        if parsed.path == "/console-api/keys":
            if not self.require_console_auth():
                return
            return json_response(self, 200, {"keys": public_keys()})
        if parsed.path == "/console-api/status":
            if not self.require_console_auth():
                return
            if urllib.parse.parse_qs(parsed.query).get("refresh") == ["1"]:
                with HEALTH_LOCK:
                    HEALTH_CACHE.clear()
            return json_response(self, 200, backend_status())
        if parsed.path == "/console-api/usage":
            if not self.require_console_auth():
                return
            return json_response(self, 200, usage_summary())
        if parsed.path == "/console-api/dashboard":
            if not self.require_console_auth():
                return
            return json_response(self, 200, website_dashboard_summary())
        if parsed.path == "/api/health":
            return json_response(self, 200, {"ok": True, "service": "wetype-asr-gateway", "features": ["asr", "search"]})
        if parsed.path == "/api/search":
            params = urllib.parse.parse_qs(parsed.query)
            data = {key: values[-1] for key, values in params.items() if values}
            if not self.require_auth(data, scope="search"):
                return
            return self.handle_search(data)
        return json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/console-api/") and parsed.path != "/console-api/control/restart":
            if not self.require_console_auth():
                return
            if not self.require_console_action():
                return
        if parsed.path == "/console-api/control/restart":
            if not self.require_console_auth():
                return
            config = asr_config()
            if not config.get("enable_restart", False):
                return json_response(self, 403, {"error": "restart_not_enabled"})
            origins = config.get("allowed_console_origins", ["http://127.0.0.1:18080"])
            allowed_hosts = {urllib.parse.urlsplit(origin).netloc for origin in origins}
            if self.headers.get("Host", "") not in allowed_hosts:
                return json_response(self, 403, {"error": "lan_console_only"})
            if self.headers.get("Origin", "") not in origins:
                return json_response(self, 403, {"error": "invalid_console_origin"})
            if not hmac.compare_digest(self.headers.get("X-Console-Action", ""), CONTROL_CSRF_TOKEN):
                return json_response(self, 403, {"error": "invalid_action_token_refresh_page"})
            try:
                data = self.read_json_or_form()
                name = data.get("service")
                targets = {"asr", "search"}
                if name not in targets:
                    return json_response(self, 400, {"error": "unsupported_service"})
                if time.monotonic() - RESTART_LAST.get(name, 0) < 60:
                    return json_response(self, 429, {"error": "restart_cooldown_60_seconds"})
                unit = "console-restart-" + name + "-" + secrets.token_hex(4)
                proc = subprocess.run(["sudo", "-n", "/usr/local/libexec/wetype-asr-restart", name], capture_output=True, text=True, timeout=8)
                if proc.returncode:
                    return json_response(self, 500, {"error": "restart_schedule_failed", "message": "Check the root-owned restart helper and restricted sudoers policy"})
                unit = proc.stdout.strip()
                RESTART_LAST[name] = time.monotonic()
                return json_response(self, 202, {"ok": True, "service": name, "unit": unit,
                    "message": "已提交重启，页面将自动检测恢复状态；请勿重复点击。"})
            except Exception as exc:
                return json_response(self, 400, {"error": type(exc).__name__})
        if parsed.path == "/console-api/keys":
            if not self.require_console_auth():
                return
            return self.create_key()
        if parsed.path.startswith("/console-api/keys/") and parsed.path.endswith("/toggle"):
            if not self.require_console_auth():
                return
            key_id = parsed.path.split("/")[-2]
            return self.toggle_key(key_id)
        if parsed.path not in {"/api/search", "/api/asr"}:
            return json_response(self, 404, {"ok": False, "error": "not_found"})
        if parsed.path == "/api/search":
            data = self.read_json_or_form()
            if not self.require_auth(data, scope="search"):
                return
            return self.handle_search(data)
        if not self.require_auth(scope="asr"):
            return
        return self.handle_asr()

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/console-api/"):
            if not self.require_console_auth() or not self.require_console_action():
                return
        if parsed.path.startswith("/console-api/keys/"):
            if not self.require_console_auth():
                return
            key_id = parsed.path.rsplit("/", 1)[-1]
            return self.delete_key(key_id)
        return json_response(self, 404, {"ok": False, "error": "not_found"})

    def handle_console(self):
        if not self.require_console_auth(redirect_on_fail=True):
            return
        return html_response(self, 200, CONSOLE_HTML)

    def handle_login(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        token = params.get("token", [""])[-1]
        if hmac.compare_digest(token, get_console_token()):
            self.send_response(303)
            self.send_header("Set-Cookie", f"console_session={token}; HttpOnly; SameSite=Lax; Path=/")
            self.send_header("Location", "/console")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return html_response(self, 401, "<!doctype html><title>Unauthorized</title><h1>Unauthorized</h1>")

    def handle_console_asset(self, request_path):
        rel = urllib.parse.unquote(request_path.removeprefix("/console-assets/fonts/"))
        if not rel or rel.startswith("/") or "\\" in rel:
            return json_response(self, 404, {"ok": False, "error": "not_found"})
        root = CONSOLE_ASSETS_DIR.resolve()
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file():
            return json_response(self, 404, {"ok": False, "error": "not_found"})
        if target.suffix == ".css":
            return static_response(self, target, "text/css; charset=utf-8")
        if target.suffix == ".woff2":
            return static_response(self, target, "font/woff2")
        return json_response(self, 404, {"ok": False, "error": "not_found"})

    def require_console_auth(self, redirect_on_fail=False):
        token = get_console_token()
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        supplied = jar.get("console_session")
        supplied_value = supplied.value if supplied else self.headers.get("X-Console-Token", "")
        if hmac.compare_digest(supplied_value, token):
            return True
        if redirect_on_fail:
            return redirect(self, "/console-login-required")
        return json_response(self, 401, {"ok": False, "error": "console_unauthorized"})

    def require_console_action(self):
        origins = asr_config().get("allowed_console_origins", ["http://127.0.0.1:18080"])
        if self.headers.get("Origin", "") not in origins or self.headers.get("Host", "") not in {urllib.parse.urlsplit(x).netloc for x in origins}:
            json_response(self, 403, {"error": "invalid_console_origin"})
            return False
        if not hmac.compare_digest(self.headers.get("X-Console-Action", ""), CONTROL_CSRF_TOKEN):
            json_response(self, 403, {"error": "invalid_action_token_refresh_page"})
            return False
        return True

    def require_auth(self, data=None, scope=None):
        supplied = self.headers.get("X-API-Key", "")
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not supplied and isinstance(data, dict):
            supplied = str(data.get("api_key", ""))
        if not find_key(supplied, scope=scope, touch=True):
            json_response(self, 401, {"ok": False, "error": "unauthorized"})
            return False
        return True

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_BODY_BYTES:
            raise ValueError("body_too_large")
        return self.rfile.read(length)

    def read_json_or_form(self):
        body = self.read_body()
        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return json.loads(body.decode("utf-8") or "{}")
        if "application/x-www-form-urlencoded" in content_type:
            parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
            return {key: values[-1] for key, values in parsed.items() if values}
        return {}

    def create_key(self):
        with KEY_LOCK:
            return self._create_key()

    def _create_key(self):
        data = self.read_json_or_form()
        name = str(data.get("name", "")).strip() or "unnamed"
        scopes = data.get("scopes", ["asr", "search"])
        if not isinstance(scopes, list):
            scopes = ["asr", "search"]
        scopes = [scope for scope in scopes if scope in {"asr", "search"}]
        if not scopes:
            return json_response(self, 400, {"ok": False, "error": "missing_scopes"})
        api_key = generate_api_key()
        store = load_store()
        item = {
            "id": secrets.token_hex(8),
            "name": name[:80],
            "sha256": key_digest(api_key),
            "masked": mask_key(api_key),
            "scopes": scopes,
            "created_at": now_iso(),
            "last_used_at": None,
            "enabled": True,
            "source": "console",
        }
        store["keys"].append(item)
        save_store(store)
        return json_response(self, 201, {"ok": True, "key": api_key, "record": public_key(item)})

    def delete_key(self, key_id):
        with KEY_LOCK:
            return self._delete_key(key_id)

    def _delete_key(self, key_id):
        store = load_store()
        before = len(store["keys"])
        store["keys"] = [item for item in store["keys"] if item.get("id") != key_id]
        if len(store["keys"]) == before:
            return json_response(self, 404, {"ok": False, "error": "not_found"})
        save_store(store)
        return json_response(self, 200, {"ok": True})

    def toggle_key(self, key_id):
        with KEY_LOCK:
            return self._toggle_key(key_id)

    def _toggle_key(self, key_id):
        store = load_store()
        for item in store["keys"]:
            if item.get("id") == key_id:
                item["enabled"] = not item.get("enabled", True)
                save_store(store)
                return json_response(self, 200, {"ok": True, "record": public_key(item)})
        return json_response(self, 404, {"ok": False, "error": "not_found"})

    def handle_search(self, data):
        try:
            return json_response(self, 200, tavily_like_search(data))
        except ValueError as exc:
            return json_response(self, 400, {"error": str(exc)})
        except Exception as exc:
            return json_response(self, 502, {"error": "search_failed", "message": str(exc)})

    def handle_asr(self):
        try:
            content_type = self.headers.get("Content-Type", "")
            language = ""
            prompt = ""
            model = ""
            suffix = ".audio"

            if "application/json" in content_type:
                data = json.loads(self.read_body().decode("utf-8") or "{}")
                encoded = data.get("audio_base64", "")
                if not encoded:
                    return json_response(self, 400, {"ok": False, "error": "missing_audio_base64"})
                audio = base64.b64decode(encoded)
                language = str(data.get("language", "")).strip()
                prompt = str(data.get("prompt", "")).strip()
                model = str(data.get("model", "")).strip()
                suffix = os.path.splitext(str(data.get("filename", "")))[1] or suffix
            elif "multipart/form-data" in content_type:
                body = self.read_body()
                environ = {
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(len(body)),
                }
                form = cgi.FieldStorage(fp=io.BytesIO(body), headers=self.headers, environ=environ)
                item = form["file"] if "file" in form else None
                if item is None or not getattr(item, "file", None):
                    return json_response(self, 400, {"ok": False, "error": "missing_file"})
                audio = item.file.read()
                language = str(form.getfirst("language", "")).strip()
                prompt = str(form.getfirst("prompt", "")).strip()
                model = str(form.getfirst("model", "")).strip()
                suffix = os.path.splitext(getattr(item, "filename", "") or "")[1] or suffix
            else:
                return json_response(self, 415, {"ok": False, "error": "unsupported_content_type"})

            if len(audio) > MAX_BODY_BYTES:
                return json_response(self, 413, {"ok": False, "error": "audio_too_large"})

            with tempfile.TemporaryDirectory(prefix="wetype-asr-") as temporary:
                audio_path = Path(temporary) / ("audio" + suffix)
                audio_path.write_bytes(audio)
                payload = run_asr(str(audio_path), language=language, prompt=prompt, model=model)
            return json_response(self, 200 if payload.get("ok") else 502, payload)
        except subprocess.TimeoutExpired:
            return json_response(self, 504, {"ok": False, "error": "asr_timeout"})
        except ValueError as exc:
            return json_response(self, 413 if str(exc) == "body_too_large" else 400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            return json_response(self, 500, {"ok": False, "error": type(exc).__name__, "message": str(exc)})


def public_key(item):
    return {
        "id": item.get("id"),
        "name": item.get("name", ""),
        "masked": item.get("masked", ""),
        "scopes": item.get("scopes", ["asr", "search"]),
        "created_at": item.get("created_at"),
        "last_used_at": item.get("last_used_at"),
        "enabled": item.get("enabled", True),
        "source": item.get("source", "console"),
    }


def public_keys():
    return [public_key(item) for item in list_keys()]


def main():
    load_store()
    get_console_token()
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"wetype-asr-gateway listening on {BIND}:{PORT}", flush=True)
    print(f"console token file: {CONSOLE_TOKEN_FILE}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
