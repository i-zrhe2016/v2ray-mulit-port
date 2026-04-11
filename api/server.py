from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from api.runtime import RuntimeSyncError, ShellV2RayRuntime
from api.service import PanelService
from api.store import StateStore
from api.subscriptions import build_clash_converter_url, build_v2ray_subscription_body, build_vmess_link, build_vmess_payload_variants


def parse_bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def strip_port(host: str) -> str:
    raw = host.strip()
    if not raw:
        return ""

    if raw.startswith("["):
        end = raw.find("]")
        if end > 1:
            return raw[1:end]

    if raw.count(":") == 1:
        return raw.split(":", 1)[0]

    return raw


def detect_server_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def resolve_request_host(headers) -> str:
    configured_host = os.getenv("V2RAY_PUBLIC_HOST", "").strip()
    if configured_host:
        return strip_port(configured_host)

    forwarded_host = headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
    direct_host = headers.get("Host", "").strip()

    for candidate in (forwarded_host, direct_host):
        parsed = strip_port(candidate)
        if parsed:
            return parsed

    return detect_server_ip()


def resolve_request_scheme(headers) -> str:
    configured_scheme = os.getenv("PANEL_PUBLIC_SCHEME", "").strip().lower()
    if configured_scheme in {"http", "https"}:
        return configured_scheme
    forwarded_proto = headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    if forwarded_proto in {"http", "https"}:
        return forwarded_proto
    return "http"


def is_local_request_host(candidate: str) -> bool:
    host = strip_port(candidate)
    if not host:
        return False
    normalized = host.strip().lower()
    if normalized in {"localhost", "0.0.0.0", "::", "::1"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def build_public_host_with_port() -> str:
    configured_host = strip_port(os.getenv("V2RAY_PUBLIC_HOST", "").strip())
    fallback_host = configured_host or detect_server_ip()
    fallback_port = str(os.getenv("API_PORT", "2016")).strip() or "2016"
    return f"{fallback_host}:{fallback_port}"


def resolve_request_host_with_port(headers) -> str:
    configured_base = os.getenv("PANEL_PUBLIC_BASE_URL", "").strip()
    if configured_base:
        return urlsplit(configured_base).netloc

    forwarded_host = headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
    direct_host = headers.get("Host", "").strip()

    for candidate in (forwarded_host, direct_host):
        if candidate:
            if is_local_request_host(candidate):
                return build_public_host_with_port()
            return candidate

    return build_public_host_with_port()


def build_request_base_url(headers) -> str:
    configured_base = os.getenv("PANEL_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured_base:
        return configured_base

    request_scheme = resolve_request_scheme(headers)
    request_host = resolve_request_host_with_port(headers)
    return f"{request_scheme}://{request_host}"


def resolve_internal_base_url() -> str:
    configured_base = os.getenv("PANEL_INTERNAL_BASE_URL", "").strip().rstrip("/")
    if configured_base:
        return configured_base
    api_port = str(os.getenv("API_PORT", "2016")).strip() or "2016"
    return f"http://127.0.0.1:{api_port}"


def resolve_subscription_variant_count() -> int:
    raw = os.getenv("V2RAY_SUBSCRIPTION_VARIANTS", "6").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 6


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict:
    content_length = int(handler.headers.get("Content-Length", "0") or "0")
    raw_body = handler.rfile.read(content_length) if content_length > 0 else b"{}"
    if not raw_body:
        return {}
    return json.loads(raw_body.decode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        request_path = urlsplit(self.path).path
        if request_path == "/":
            self.redirect("/admin")
            return
        if request_path == "/admin":
            self.respond_html(200, render_admin_page())
            return
        if request_path == "/api/ports":
            self.respond_json(
                200,
                {
                    "ports": self.server.panel_service.list_ports(
                        self.request_base_url,
                        self.public_host,
                    ),
                },
            )
            return
        if request_path.startswith("/links/"):
            self.handle_links_lookup(request_path.rsplit("/", 1)[-1])
            return
        if request_path.startswith("/api/links/"):
            self.handle_links_lookup(request_path.rsplit("/", 1)[-1])
            return
        if request_path.startswith("/subscriptions/"):
            self.handle_subscription_get(request_path)
            return
        if request_path == "/healthz":
            self.respond_json(200, {"status": "ok"})
            return
        self.respond_json(404, {"error": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        request_path = urlsplit(self.path).path
        if request_path == "/api/ports":
            self.handle_create_port()
            return
        if request_path.endswith("/reset-traffic"):
            port = self.extract_port(request_path, "/api/ports/", "/reset-traffic")
            self.handle_reset_traffic(port)
            return
        if request_path.endswith("/sync"):
            port = self.extract_port(request_path, "/api/ports/", "/sync")
            self.handle_sync_port(port)
            return
        if request_path == "/api/sync":
            self.server.panel_service.sync_all()
            self.respond_json(200, {"ok": True})
            return
        self.respond_json(404, {"error": "Not Found"})

    def do_PATCH(self) -> None:  # noqa: N802
        request_path = urlsplit(self.path).path
        if request_path.startswith("/api/ports/"):
            port = self.extract_port(request_path, "/api/ports/", "")
            self.handle_update_port(port)
            return
        self.respond_json(404, {"error": "Not Found"})

    def do_DELETE(self) -> None:  # noqa: N802
        request_path = urlsplit(self.path).path
        if request_path.startswith("/api/ports/"):
            port = self.extract_port(request_path, "/api/ports/", "")
            self.handle_delete_port(port)
            return
        self.respond_json(404, {"error": "Not Found"})

    @property
    def request_base_url(self) -> str:
        return build_request_base_url(self.headers)

    @property
    def public_host(self) -> str:
        return resolve_request_host(self.headers)

    def handle_create_port(self) -> None:
        try:
            payload = parse_json_body(self)
            record = self.server.panel_service.create_port(
                payload,
                self.request_base_url,
                self.public_host,
            )
            self.respond_json(201, record)
        except ValueError as exc:
            self.respond_json(400, {"error": str(exc)})
        except RuntimeSyncError as exc:
            self.respond_json(500, {"error": str(exc)})

    def handle_update_port(self, port: int) -> None:
        try:
            payload = parse_json_body(self)
            record = self.server.panel_service.update_port(
                port,
                payload,
                self.request_base_url,
                self.public_host,
            )
            self.respond_json(200, record)
        except KeyError as exc:
            self.respond_json(404, {"error": str(exc)})
        except ValueError as exc:
            self.respond_json(400, {"error": str(exc)})
        except RuntimeSyncError as exc:
            self.respond_json(500, {"error": str(exc)})

    def handle_delete_port(self, port: int) -> None:
        try:
            self.server.panel_service.delete_port(port)
            self.respond_json(200, {"ok": True})
        except KeyError as exc:
            self.respond_json(404, {"error": str(exc)})

    def handle_reset_traffic(self, port: int) -> None:
        try:
            record = self.server.panel_service.reset_traffic(
                port,
                self.request_base_url,
                self.public_host,
            )
            self.respond_json(200, record)
        except KeyError as exc:
            self.respond_json(404, {"error": str(exc)})
        except RuntimeSyncError as exc:
            self.respond_json(500, {"error": str(exc)})

    def handle_sync_port(self, port: int) -> None:
        try:
            record = self.server.panel_service.sync_port(
                port,
                self.request_base_url,
                self.public_host,
            )
            self.respond_json(200, record)
        except KeyError as exc:
            self.respond_json(404, {"error": str(exc)})
        except RuntimeSyncError as exc:
            self.respond_json(500, {"error": str(exc)})

    def handle_links_lookup(self, token: str) -> None:
        try:
            record = self.server.panel_service.get_links_by_token(
                token,
                self.request_base_url,
                self.public_host,
            )
        except KeyError:
            self.respond_json(404, {"error": "Not Found"})
            return
        self.respond_json(
            200,
            {
                "port": record["port"],
                "remark": record["remark"],
                "status": record["status"],
                "traffic_used_bytes": record["traffic_used_bytes"],
                "traffic_limit_bytes": record["traffic_limit_bytes"],
                "expires_at": record["expires_at"],
                "vmess_link": record["vmess_link"],
                "subscriptions": record["links"],
            },
        )

    def handle_subscription_get(self, request_path: str) -> None:
        parts = [part for part in request_path.split("/") if part]
        if len(parts) != 3:
            self.respond_json(404, {"error": "Not Found"})
            return
        _, token, target = parts
        try:
            record = self.server.panel_service.get_port_by_token(token)
            links = self.server.panel_service.get_links_by_token(
                token,
                self.request_base_url,
                self.public_host,
            )
        except KeyError:
            self.respond_json(404, {"error": "Not Found"})
            return

        if target == "v2ray":
            if record["status"] != "active":
                self.respond_json(403, {"error": f"subscription is {record['status']}"})
                return
            vmess_links = [
                build_vmess_link(payload)
                for payload in build_vmess_payload_variants(
                    record,
                    self.public_host,
                    self.server.panel_service.tls_enabled,
                    resolve_subscription_variant_count(),
                )
            ]
            body = build_v2ray_subscription_body(vmess_links)
            self.respond_text(200, body, "text/plain; charset=utf-8")
            return

        if target != "clash":
            self.respond_json(404, {"error": "Not Found"})
            return

        if record["status"] != "active":
            self.respond_json(403, {"error": f"subscription is {record['status']}"})
            return

        source_url = f"{resolve_internal_base_url()}/subscriptions/{token}/v2ray"
        converter_base_url = os.getenv("SUBCONVERTER_INTERNAL_URL", "http://subconverter:25500/sub").strip()
        template_url = os.getenv("V2RAY_SUBCONVERTER_TEMPLATE", "").strip()
        try:
            clash_url = build_clash_converter_url(source_url, converter_base_url, template_url)
            request = urllib.request.Request(clash_url, headers={"User-Agent": "v2ray-panel/1.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type", "text/yaml; charset=utf-8")
            self.respond_bytes(200, body, content_type)
        except (ValueError, urllib.error.URLError) as exc:
            self.respond_json(502, {"error": f"failed to build clash subscription: {exc}"})

    def log_message(self, format: str, *args: object) -> None:
        return

    def respond_json(self, status: int, body: dict) -> None:
        content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def respond_html(self, status: int, body: str) -> None:
        self.respond_text(status, body, "text/html; charset=utf-8")

    def respond_text(self, status: int, body: str, content_type: str) -> None:
        self.respond_bytes(status, body.encode("utf-8"), content_type)

    def respond_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def extract_port(self, path: str, prefix: str, suffix: str) -> int:
        trimmed = path[len(prefix) :]
        if suffix and trimmed.endswith(suffix):
            trimmed = trimmed[: -len(suffix)]
        trimmed = trimmed.strip("/")
        return int(trimmed)


class PanelHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, panel_service: PanelService, sync_interval: int) -> None:
        super().__init__(server_address, handler_class)
        self.panel_service = panel_service
        self.sync_interval = sync_interval
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)

    def start_background_sync(self) -> None:
        self._sync_thread.start()

    def _sync_loop(self) -> None:
        while True:
            try:
                self.panel_service.sync_all()
            except Exception:
                pass
            threading.Event().wait(self.sync_interval)


def render_admin_page() -> str:
    port_range_start = int(os.getenv("V2RAY_PORT_RANGE_START", "20000"))
    port_range_end = int(os.getenv("V2RAY_PORT_RANGE_END", "20100"))
    html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>V2Ray 多端口管理面板</title>
  <style>
    :root {{
      --bg: #f4efe7;
      --panel: rgba(255, 252, 247, 0.88);
      --ink: #1f2933;
      --muted: #66788a;
      --line: rgba(31, 41, 51, 0.12);
      --accent: #d16a28;
      --accent-soft: rgba(209, 106, 40, 0.12);
      --ok: #13795b;
      --warn: #b36b00;
      --bad: #bf2f45;
      --shadow: 0 18px 40px rgba(87, 69, 44, 0.14);
      font-family: "IBM Plex Sans", "Avenir Next", "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(209,106,40,0.14), transparent 26rem),
        radial-gradient(circle at top right, rgba(19,121,91,0.10), transparent 22rem),
        linear-gradient(180deg, #f8f3eb 0%, var(--bg) 100%);
      min-height: 100vh;
    }}
    .shell {{
      max-width: 1240px;
      margin: 0 auto;
      padding: 28px 16px 48px;
    }}
    .hero {{
      display: grid;
      gap: 18px;
      grid-template-columns: 1.2fr 0.8fr;
      margin-bottom: 24px;
    }}
    .hero-card, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 22px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: clamp(30px, 4vw, 46px);
      line-height: 1.02;
      letter-spacing: -0.04em;
    }}
    p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-top: 20px;
    }}
    .metric {{
      padding: 14px 16px;
      border-radius: 16px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.7);
    }}
    .metric b {{
      display: block;
      font-size: 26px;
      margin-bottom: 4px;
    }}
    .toolbar {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 18px;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 11px 16px;
      font: inherit;
      cursor: pointer;
      background: var(--accent);
      color: white;
    }}
    button.secondary {{
      background: rgba(31,41,51,0.08);
      color: var(--ink);
    }}
    button.warn {{ background: var(--warn); }}
    button.bad {{ background: var(--bad); }}
    button.ok {{ background: var(--ok); }}
    .cards {{
      display: grid;
      gap: 14px;
      margin-top: 18px;
    }}
    .field-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    label {{
      display: grid;
      gap: 6px;
      font-size: 13px;
      color: var(--muted);
    }}
    input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px 12px;
      font: inherit;
      color: var(--ink);
      background: rgba(255,255,255,0.9);
    }}
    .port-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 18px;
      box-shadow: var(--shadow);
    }}
    .port-top {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 14px;
    }}
    .chip {{
      padding: 7px 11px;
      border-radius: 999px;
      font-size: 12px;
      background: var(--accent-soft);
      color: var(--ink);
      white-space: nowrap;
    }}
    .status-active {{ background: rgba(19,121,91,0.14); color: var(--ok); }}
    .status-disabled {{ background: rgba(102,120,138,0.14); color: #5a6b7c; }}
    .status-expired, .status-exhausted, .status-sync_error {{ background: rgba(191,47,69,0.14); color: var(--bad); }}
    .meta {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .meta div {{
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 10px 12px;
      background: rgba(255,255,255,0.68);
    }}
    .meta b {{
      display: block;
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 4px;
      font-weight: 500;
    }}
    .links {{
      display: grid;
      gap: 8px;
      margin: 14px 0;
    }}
    .links a {{
      color: var(--accent);
      text-decoration: none;
      word-break: break-all;
    }}
    .row-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }}
    .muted {{
      color: var(--muted);
      font-size: 13px;
    }}
    @media (max-width: 900px) {{
      .hero {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="hero-card">
        <h1>V2Ray 多端口面板</h1>
        <p>一个端口对应一个订阅。面板负责端口创建、删除、流量统计、到期禁用、Clash/V2Ray 订阅和运行时同步。</p>
        <div class="metrics">
          <div class="metric"><b id="metric-total">0</b><span>总端口</span></div>
          <div class="metric"><b id="metric-active">0</b><span>活跃</span></div>
          <div class="metric"><b id="metric-traffic">0 B</b><span>累计已用流量</span></div>
        </div>
        <div class="toolbar">
          <button onclick="syncAll()">立即全量同步</button>
          <button class="secondary" onclick="loadPorts()">刷新列表</button>
        </div>
      </div>
      <div class="panel">
        <h2>添加端口</h2>
        <p class="muted" style="margin-bottom:14px;">可用端口范围: __PORT_RANGE_START__ - __PORT_RANGE_END__</p>
        <div class="field-grid">
          <label>端口<input id="add-port" type="number" min="__PORT_RANGE_START__" max="__PORT_RANGE_END__" required></label>
          <label>备注<input id="add-remark" type="text" placeholder="user-20001"></label>
          <label>流量上限 GB<input id="add-limit" type="number" min="0.01" step="0.01" value="100"></label>
          <label>到期时间<input id="add-expire" type="datetime-local"></label>
          <label>UUID<input id="add-uuid" type="text" placeholder="留空自动生成"></label>
          <label>WS 路径<input id="add-wspath" type="text" placeholder="/ws/20001"></label>
        </div>
        <div class="toolbar">
          <button onclick="createPort()">创建端口</button>
        </div>
        <p class="muted" id="message"></p>
      </div>
    </section>
    <section class="cards" id="port-list"></section>
  </div>
  <script>
    const bytesPerGb = 1024 ** 3;
    const portRangeStart = __PORT_RANGE_START__;
    const portRangeEnd = __PORT_RANGE_END__;

    function formatBytes(value) {{
      if (!Number.isFinite(value)) return "0 B";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let amount = value;
      let index = 0;
      while (amount >= 1024 && index < units.length - 1) {{
        amount /= 1024;
        index += 1;
      }}
      return `${{amount.toFixed(amount >= 10 || index === 0 ? 0 : 2)}} ${{units[index]}}`;
    }}

    function isoToLocalValue(value) {{
      if (!value) return "";
      const date = new Date(value);
      const pad = (item) => String(item).padStart(2, "0");
      return `${{date.getFullYear()}}-${{pad(date.getMonth()+1)}}-${{pad(date.getDate())}}T${{pad(date.getHours())}}:${{pad(date.getMinutes())}}`;
    }}

    function localValueToIso(value) {{
      if (!value) return "";
      return new Date(value).toISOString();
    }}

    function setMessage(text, isError = false) {{
      const element = document.getElementById("message");
      element.textContent = text;
      element.style.color = isError ? "var(--bad)" : "var(--muted)";
    }}

    async function api(url, options = {{}}) {{
      const response = await fetch(url, {{
        headers: {{
          "Content-Type": "application/json",
        }},
        ...options,
      }});
      const data = await response.json().catch(() => ({{}}));
      if (!response.ok) {{
        throw new Error(data.error || `HTTP ${{response.status}}`);
      }}
      return data;
    }}

    function renderMetrics(ports) {{
      const total = ports.length;
      const active = ports.filter((port) => port.status === "active").length;
      const used = ports.reduce((sum, port) => sum + Number(port.traffic_used_bytes || 0), 0);
      document.getElementById("metric-total").textContent = String(total);
      document.getElementById("metric-active").textContent = String(active);
      document.getElementById("metric-traffic").textContent = formatBytes(used);
    }}

    function buildCard(port) {{
      const limitGb = (Number(port.traffic_limit_bytes) / bytesPerGb).toFixed(2);
      const expiresValue = isoToLocalValue(port.expires_at);
      const lastError = port.last_sync_error || "";
      return `
        <article class="port-card">
          <div class="port-top">
            <div>
              <div class="muted">端口 ${{port.port}}</div>
              <h2 style="margin:4px 0 0;font-size:24px;">${{port.remark}}</h2>
            </div>
            <span class="chip status-${port.status}">${port.status}</span>
          </div>
          <div class="meta">
            <div><b>已用 / 上限</b>${{formatBytes(Number(port.traffic_used_bytes))}} / ${{formatBytes(Number(port.traffic_limit_bytes))}}</div>
            <div><b>剩余流量</b>${{formatBytes(Number(port.traffic_remaining_bytes))}}</div>
            <div><b>到期时间</b>${{port.expires_at || "未设置"}}</div>
            <div><b>WS 路径</b>${{port.ws_path}}</div>
          </div>
          <div class="links">
            <a href="${{port.links.v2ray}}" target="_blank" rel="noreferrer">V2Ray 订阅: ${{port.links.v2ray}}</a>
            <a href="${{port.links.clash}}" target="_blank" rel="noreferrer">Clash 订阅: ${{port.links.clash}}</a>
            <a href="${{port.links.info}}" target="_blank" rel="noreferrer">链接信息: ${{port.links.info}}</a>
          </div>
          <div class="field-grid">
            <label>备注<input id="remark-${{port.port}}" type="text" value="${{port.remark}}"></label>
            <label>流量上限 GB<input id="limit-${{port.port}}" type="number" min="0.01" step="0.01" value="${{limitGb}}"></label>
            <label>到期时间<input id="expire-${{port.port}}" type="datetime-local" value="${{expiresValue}}"></label>
            <label>启用状态<input id="enabled-${{port.port}}" type="text" value="${{port.enabled ? "true" : "false"}}"></label>
          </div>
          <div class="row-actions">
            <button onclick="savePort(${{port.port}})">保存</button>
            <button class="${{port.enabled ? "warn" : "ok"}}" onclick="togglePort(${{port.port}}, ${{!port.enabled}})">${{port.enabled ? "禁用" : "启用"}}</button>
            <button class="secondary" onclick="resetTraffic(${{port.port}})">流量清零</button>
            <button class="secondary" onclick="syncPort(${{port.port}})">同步</button>
            <button class="bad" onclick="deletePort(${{port.port}})">删除</button>
          </div>
          <p class="muted" style="margin-top:10px;">最后同步: ${{port.last_synced_at || "尚未同步"}}${{lastError ? ` | 错误: ${{lastError}}` : ""}}</p>
        </article>
      `;
    }}

    async function loadPorts() {{
      const data = await api("/api/ports");
      const ports = data.ports || [];
      renderMetrics(ports);
      const container = document.getElementById("port-list");
      container.innerHTML = ports.map(buildCard).join("") || '<div class="panel"><h2>暂无端口</h2><p class="muted">先在右上角创建一个端口。</p></div>';
    }}

    async function createPort() {{
      const port = Number(document.getElementById("add-port").value);
      if (!Number.isInteger(port) || port < portRangeStart || port > portRangeEnd) {{
        setMessage(`端口必须在 ${{portRangeStart}}-${{portRangeEnd}} 之间`, true);
        return;
      }}
      const payload = {{
        port,
        remark: document.getElementById("add-remark").value.trim(),
        traffic_limit_bytes: Math.round(Number(document.getElementById("add-limit").value || "0") * bytesPerGb),
        expires_at: localValueToIso(document.getElementById("add-expire").value),
        uuid: document.getElementById("add-uuid").value.trim(),
        ws_path: document.getElementById("add-wspath").value.trim(),
      }};
      try {{
        await api("/api/ports", {{ method: "POST", body: JSON.stringify(payload) }});
        setMessage("端口已创建");
        await loadPorts();
      }} catch (error) {{
        setMessage(error.message, true);
      }}
    }}

    async function savePort(port) {{
      const payload = {{
        remark: document.getElementById(`remark-${{port}}`).value.trim(),
        traffic_limit_bytes: Math.round(Number(document.getElementById(`limit-${{port}}`).value || "0") * bytesPerGb),
        expires_at: localValueToIso(document.getElementById(`expire-${{port}}`).value),
        enabled: document.getElementById(`enabled-${{port}}`).value.trim().toLowerCase() === "true",
      }};
      try {{
        await api(`/api/ports/${{port}}`, {{ method: "PATCH", body: JSON.stringify(payload) }});
        await loadPorts();
      }} catch (error) {{
        setMessage(error.message, true);
      }}
    }}

    async function togglePort(port, enabled) {{
      try {{
        await api(`/api/ports/${{port}}`, {{
          method: "PATCH",
          body: JSON.stringify({{ enabled }}),
        }});
        await loadPorts();
      }} catch (error) {{
        setMessage(error.message, true);
      }}
    }}

    async function resetTraffic(port) {{
      try {{
        await api(`/api/ports/${{port}}/reset-traffic`, {{ method: "POST", body: "{}" }});
        await loadPorts();
      }} catch (error) {{
        setMessage(error.message, true);
      }}
    }}

    async function syncPort(port) {{
      try {{
        await api(`/api/ports/${{port}}/sync`, {{ method: "POST", body: "{}" }});
        await loadPorts();
      }} catch (error) {{
        setMessage(error.message, true);
      }}
    }}

    async function syncAll() {{
      try {{
        await api("/api/sync", {{ method: "POST", body: "{}" }});
        await loadPorts();
      }} catch (error) {{
        setMessage(error.message, true);
      }}
    }}

    async function deletePort(port) {{
      if (!window.confirm(`确认删除端口 ${{port}}？`)) return;
      try {{
        await api(`/api/ports/${{port}}`, {{ method: "DELETE" }});
        await loadPorts();
      }} catch (error) {{
        setMessage(error.message, true);
      }}
    }}

    document.getElementById("add-expire").value = isoToLocalValue(new Date(Date.now() + 30 * 24 * 3600 * 1000).toISOString());
    loadPorts().catch((error) => setMessage(error.message, true));
  </script>
</body>
</html>"""
    normalized = html.replace("{{", "{").replace("}}", "}")
    return (
        normalized.replace("__PORT_RANGE_START__", str(port_range_start))
        .replace("__PORT_RANGE_END__", str(port_range_end))
    )


def main() -> None:
    listen_port = int(os.getenv("API_PORT", "2016"))
    sync_interval = int(os.getenv("SYNC_INTERVAL_SECONDS", "30"))
    port_range_start = int(os.getenv("V2RAY_PORT_RANGE_START", "20000"))
    port_range_end = int(os.getenv("V2RAY_PORT_RANGE_END", "20100"))
    panel_port = listen_port
    v2ray_api_port = int(os.getenv("V2RAY_API_PORT", "10085"))
    runtime_enabled = parse_bool(os.getenv("V2RAY_RUNTIME_ENABLED", "true"))
    runtime = ShellV2RayRuntime(
        command=os.getenv("V2RAY_API_COMMAND", "v2ray"),
        server=os.getenv("V2RAY_API_SERVER", f"127.0.0.1:{v2ray_api_port}"),
        timeout_seconds=int(os.getenv("V2RAY_API_TIMEOUT_SECONDS", "10")),
        enabled=runtime_enabled,
    )
    panel_service = PanelService(
        store=StateStore(os.getenv("STATE_FILE", "/data/ports.json")),
        runtime_client=runtime,
        port_range_start=port_range_start,
        port_range_end=port_range_end,
        reserved_ports={panel_port, v2ray_api_port},
        tls_enabled=parse_bool(os.getenv("V2RAY_PUBLIC_TLS", "false")),
    )
    panel_service.initialize()
    server = PanelHTTPServer(("0.0.0.0", listen_port), Handler, panel_service, sync_interval)
    server.start_background_sync()
    server.serve_forever()


if __name__ == "__main__":
    main()
