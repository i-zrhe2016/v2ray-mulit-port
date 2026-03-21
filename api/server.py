import base64
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer


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
    forwarded_host = headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
    direct_host = headers.get("Host", "").strip()

    for candidate in (forwarded_host, direct_host):
        parsed = strip_port(candidate)
        if parsed:
            return parsed

    return detect_server_ip()


def build_vmess_payload(host_override: str = "") -> dict[str, str]:
    v2ray_uuid = os.getenv("V2RAY_UUID", "").strip()
    if not v2ray_uuid:
        raise ValueError("V2RAY_UUID is required")

    v2ray_port = str(os.getenv("V2RAY_PORT", "10086")).strip()
    v2ray_alter_id = str(os.getenv("V2RAY_ALTER_ID", "0")).strip()
    v2ray_ws_path = os.getenv("V2RAY_WS_PATH", "/ray").strip() or "/ray"
    configured_host = os.getenv("V2RAY_API_HOST", "").strip()
    v2ray_host = configured_host or host_override.strip() or detect_server_ip()
    v2ray_remark = os.getenv("V2RAY_API_REMARK", "v2ray-ws").strip() or "v2ray-ws"
    v2ray_tls = parse_bool(os.getenv("V2RAY_API_TLS", "false").strip())

    return {
        "v": "2",
        "ps": v2ray_remark,
        "add": v2ray_host,
        "port": v2ray_port,
        "id": v2ray_uuid,
        "aid": v2ray_alter_id,
        "scy": "auto",
        "net": "ws",
        "type": "none",
        "host": "",
        "path": v2ray_ws_path,
        "tls": "tls" if v2ray_tls else "",
    }


def build_vmess_link(payload: dict[str, str]) -> str:
    payload_json = json.dumps(payload, separators=(",", ":"))
    encoded = base64.b64encode(payload_json.encode("utf-8")).decode("utf-8")
    return f"vmess://{encoded}"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/api/v2ray":
            self.respond_json(404, {"error": "Not Found"})
            return

        try:
            request_host = resolve_request_host(self.headers)
            payload = build_vmess_payload(request_host)
            link = build_vmess_link(payload)
        except ValueError as exc:
            self.respond_json(500, {"error": str(exc)})
            return

        self.respond_json(
            200,
            {
                "link": link,
                "config": payload,
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        return

    def respond_json(self, status: int, body: dict) -> None:
        content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main() -> None:
    listen_port = int(os.getenv("API_PORT", "2016"))
    server = HTTPServer(("0.0.0.0", listen_port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
