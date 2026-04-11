from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from . import models


class StateValidationError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StateValidationError(message)


def validate_state(state: dict[str, Any]) -> dict[str, Any]:
    _require(isinstance(state, dict), "state must be a JSON object")

    version = int(state.get("version", 1))
    _require(version == 1, "unsupported state version")

    server = state.get("server", {})
    _require(isinstance(server, dict), "server must be an object")

    ports = state.get("ports", [])
    _require(isinstance(ports, list), "ports must be an array")

    normalized_ports = []
    seen_ports: set[int] = set()
    seen_uuids: set[str] = set()
    seen_paths: set[str] = set()
    seen_tokens: set[str] = set()

    for raw in ports:
        _require(isinstance(raw, dict), "port record must be an object")
        record = models.ensure_port_record(raw)

        _require(record["port"] not in seen_ports, f"duplicate port: {record['port']}")
        _require(record["uuid"] not in seen_uuids, f"duplicate uuid: {record['uuid']}")
        _require(record["ws_path"] not in seen_paths, f"duplicate ws_path: {record['ws_path']}")
        _require(
            record["subscription_token"] not in seen_tokens,
            f"duplicate subscription_token: {record['subscription_token']}",
        )
        _require(record["uuid"], f"uuid is required for port {record['port']}")
        _require(record["remark"], f"remark is required for port {record['port']}")
        _require(record["subscription_token"], f"subscription_token is required for port {record['port']}")
        _require(record["traffic_limit_bytes"] > 0, f"traffic_limit_bytes must be positive for port {record['port']}")
        _require(record["traffic_used_bytes"] >= 0, f"traffic_used_bytes must be non-negative for port {record['port']}")
        _require(
            record["traffic_reset_base_bytes"] >= 0,
            f"traffic_reset_base_bytes must be non-negative for port {record['port']}",
        )

        if record["expires_at"]:
            models.parse_timestamp(record["expires_at"])
        models.parse_timestamp(record["created_at"])
        models.parse_timestamp(record["updated_at"])
        if record["last_synced_at"]:
            models.parse_timestamp(record["last_synced_at"])
        _require(record["status"] in models.VALID_STATUSES, f"invalid status for port {record['port']}")

        seen_ports.add(record["port"])
        seen_uuids.add(record["uuid"])
        seen_paths.add(record["ws_path"])
        seen_tokens.add(record["subscription_token"])
        normalized_ports.append(record)

    return {
        "version": version,
        "server": server,
        "ports": sorted(normalized_ports, key=lambda item: item["port"]),
    }


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return models.fresh_state()

        with open(self.path, "r", encoding="utf-8") as handle:
            raw_state = json.load(handle)

        return validate_state(raw_state)

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_state(state)
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)

        fd, temp_path = tempfile.mkstemp(prefix=".ports.", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(normalized, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

        return normalized
