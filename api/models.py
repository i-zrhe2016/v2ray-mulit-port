from __future__ import annotations

import copy
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUS_EXPIRED = "expired"
STATUS_EXHAUSTED = "exhausted"
STATUS_SYNC_ERROR = "sync_error"
VALID_STATUSES = {
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_EXPIRED,
    STATUS_EXHAUSTED,
    STATUS_SYNC_ERROR,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    raw = value.strip()
    if not raw:
        raise ValueError("timestamp is required")
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_optional_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not value.strip():
        return None
    return parse_timestamp(value)


def normalize_ws_path(value: str, port: int) -> str:
    raw = value.strip() if value else ""
    if not raw:
        raw = f"/ws/{port}"
    if not raw.startswith("/"):
        raw = f"/{raw}"
    return raw


def build_port_tag(port: int) -> str:
    return f"managed-{port}"


def build_port_email(port: int) -> str:
    return f"port-{port}@panel.local"


def build_default_remark(port: int) -> str:
    return f"user-{port}"


def default_expires_at(now: datetime | None = None) -> str:
    current = now or utc_now()
    return format_timestamp(current + timedelta(days=30))


def generate_uuid() -> str:
    return str(uuid.uuid4())


def generate_subscription_token() -> str:
    return secrets.token_urlsafe(24)


def derive_rule_status(record: dict[str, Any], now: datetime | None = None) -> str:
    current = now or utc_now()

    if not bool(record.get("enabled", True)):
        return STATUS_DISABLED

    expires_at = parse_optional_timestamp(record.get("expires_at", ""))
    if expires_at is not None and current >= expires_at:
        return STATUS_EXPIRED

    traffic_limit = int(record.get("traffic_limit_bytes", 0))
    traffic_used = int(record.get("traffic_used_bytes", 0))
    if traffic_limit > 0 and traffic_used >= traffic_limit:
        return STATUS_EXHAUSTED

    return STATUS_ACTIVE


def derive_status(record: dict[str, Any], now: datetime | None = None) -> str:
    base_status = derive_rule_status(record, now)
    if base_status != STATUS_ACTIVE:
        return base_status
    if record.get("last_sync_error", "").strip():
        return STATUS_SYNC_ERROR
    return STATUS_ACTIVE


def ensure_port_record(record: dict[str, Any]) -> dict[str, Any]:
    port = int(record["port"])
    now = utc_now()
    created_at = record.get("created_at") or format_timestamp(now)
    updated_at = record.get("updated_at") or created_at
    expires_at = record.get("expires_at", "").strip()
    normalized = {
        "port": port,
        "uuid": str(record["uuid"]).strip(),
        "remark": str(record.get("remark") or build_default_remark(port)).strip(),
        "ws_path": normalize_ws_path(str(record.get("ws_path", "")), port),
        "alter_id": int(record.get("alter_id", 0)),
        "enabled": bool(record.get("enabled", True)),
        "status": str(record.get("status") or STATUS_ACTIVE).strip() or STATUS_ACTIVE,
        "traffic_limit_bytes": int(record["traffic_limit_bytes"]),
        "traffic_used_bytes": int(record.get("traffic_used_bytes", 0)),
        "traffic_reset_base_bytes": int(record.get("traffic_reset_base_bytes", 0)),
        "expires_at": expires_at,
        "created_at": created_at,
        "updated_at": updated_at,
        "last_synced_at": str(record.get("last_synced_at", "")).strip(),
        "last_sync_error": str(record.get("last_sync_error", "")).strip(),
        "subscription_token": str(record["subscription_token"]).strip(),
    }
    normalized["status"] = derive_status(normalized, now)
    return normalized


def make_port_record(payload: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    current = now or utc_now()
    port = int(payload["port"])
    record = {
        "port": port,
        "uuid": str(payload.get("uuid") or generate_uuid()),
        "remark": str(payload.get("remark") or build_default_remark(port)).strip(),
        "ws_path": normalize_ws_path(str(payload.get("ws_path", "")), port),
        "alter_id": int(payload.get("alter_id", 0)),
        "enabled": bool(payload.get("enabled", True)),
        "status": STATUS_ACTIVE,
        "traffic_limit_bytes": int(payload["traffic_limit_bytes"]),
        "traffic_used_bytes": int(payload.get("traffic_used_bytes", 0)),
        "traffic_reset_base_bytes": int(payload.get("traffic_reset_base_bytes", 0)),
        "expires_at": str(payload.get("expires_at") or default_expires_at(current)).strip(),
        "created_at": format_timestamp(current),
        "updated_at": format_timestamp(current),
        "last_synced_at": "",
        "last_sync_error": "",
        "subscription_token": str(
            payload.get("subscription_token") or generate_subscription_token(),
        ).strip(),
    }
    return ensure_port_record(record)


def fresh_state() -> dict[str, Any]:
    return {
        "version": 1,
        "server": {},
        "ports": [],
    }


def clone_state(state: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(state)


def human_bytes(value: int) -> str:
    negative = value < 0
    amount = float(abs(value))
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            formatted = f"{amount:.2f}".rstrip("0").rstrip(".")
            prefix = "-" if negative else ""
            return f"{prefix}{formatted} {unit}"
        amount /= 1024

    return f"{value} B"
