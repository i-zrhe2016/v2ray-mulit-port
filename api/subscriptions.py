from __future__ import annotations

import base64
import json
from urllib.parse import urlencode


def build_vmess_payload(record: dict, public_host: str, tls_enabled: bool) -> dict[str, str]:
    return {
        "v": "2",
        "ps": record["remark"],
        "add": public_host,
        "port": str(record["port"]),
        "id": record["uuid"],
        "aid": str(record.get("alter_id", 0)),
        "scy": "auto",
        "net": "ws",
        "type": "none",
        "host": "",
        "path": record["ws_path"],
        "tls": "tls" if tls_enabled else "",
    }


def build_vmess_link(payload: dict[str, str]) -> str:
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    encoded = base64.b64encode(payload_json.encode("utf-8")).decode("utf-8")
    return f"vmess://{encoded}"


def build_v2ray_subscription_body(vmess_link: str) -> str:
    content = f"{vmess_link}\n".encode("utf-8")
    return base64.b64encode(content).decode("utf-8")


def build_clash_converter_url(
    source_subscription_url: str,
    converter_base_url: str,
    template_url: str = "",
) -> str:
    source_url = source_subscription_url.strip()
    converter_url = converter_base_url.strip()
    if not source_url:
        raise ValueError("source_subscription_url is required")
    if not converter_url:
        raise ValueError("converter_base_url is required")

    query = {
        "target": "clash",
        "url": source_url,
        "insert": "false",
    }
    if template_url.strip():
        query["config"] = template_url.strip()

    delimiter = "&" if "?" in converter_url else "?"
    return f"{converter_url}{delimiter}{urlencode(query)}"
