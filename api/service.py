from __future__ import annotations

import threading
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from . import models
from .runtime import RuntimeSyncError
from .settings import apply_server_settings_patch, default_server_settings, normalize_server_settings
from .store import StateStore
from .subscriptions import build_vmess_link, build_vmess_payload


TrafficClientFactory = Callable[[Any, dict[str, Any]], Any]


class PanelService:
    def __init__(
        self,
        store: StateStore,
        runtime_client,
        port_range_start: int,
        port_range_end: int,
        reserved_ports: set[int] | None = None,
        nginx_port_manager=None,
        traffic_client=None,
        traffic_client_factory: TrafficClientFactory | None = None,
        tls_enabled: bool = False,
        server_settings_defaults: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.runtime_client = runtime_client
        self.nginx_port_manager = nginx_port_manager
        self.traffic_client = traffic_client or runtime_client
        self.traffic_client_factory = traffic_client_factory
        self.port_range_start = port_range_start
        self.port_range_end = port_range_end
        self.reserved_ports = reserved_ports or set()
        self.tls_enabled = tls_enabled
        self.lock = threading.RLock()
        self.server_settings_defaults = normalize_server_settings(
            server_settings_defaults or default_server_settings(),
        )
        self.state = self.store.load()
        self._ensure_server_settings()
        self._apply_server_settings()

    def initialize(self) -> None:
        self.sync_all()

    def list_ports(self, base_url: str, public_host: str) -> list[dict[str, Any]]:
        with self.lock:
            records = [self._serialize_record(record, base_url, public_host) for record in self.state["ports"]]
        return sorted(records, key=lambda item: item["port"])

    def get_links_by_token(self, token: str, base_url: str, public_host: str) -> dict[str, Any]:
        with self.lock:
            record = self._get_record_by_token(token)
            return self._serialize_record(record, base_url, public_host)

    def get_port_by_token(self, token: str) -> dict[str, Any]:
        with self.lock:
            return dict(self._get_record_by_token(token))

    def create_port(self, payload: dict[str, Any], base_url: str, public_host: str) -> dict[str, Any]:
        with self.lock:
            previous_state = deepcopy(self.state)
            record = models.make_port_record(payload)
            self._validate_new_record(record)
            self.state["ports"].append(record)
            self.state["ports"].sort(key=lambda item: item["port"])
            self._sync_and_persist(previous_state)
            return self._serialize_record(self._get_record(record["port"]), base_url, public_host)

    def update_port(
        self,
        port: int,
        payload: dict[str, Any],
        base_url: str,
        public_host: str,
    ) -> dict[str, Any]:
        with self.lock:
            previous_state = deepcopy(self.state)
            record = self._get_record(port)

            if "remark" in payload:
                remark = str(payload["remark"]).strip()
                if not remark:
                    raise ValueError("remark is required")
                record["remark"] = remark

            if "enabled" in payload:
                record["enabled"] = bool(payload["enabled"])

            if "traffic_limit_bytes" in payload:
                traffic_limit_bytes = int(payload["traffic_limit_bytes"])
                if traffic_limit_bytes <= 0:
                    raise ValueError("traffic_limit_bytes must be positive")
                record["traffic_limit_bytes"] = traffic_limit_bytes

            if "expires_at" in payload:
                expires_at = str(payload["expires_at"]).strip()
                if expires_at:
                    models.parse_timestamp(expires_at)
                record["expires_at"] = expires_at

            record["updated_at"] = models.format_timestamp(models.utc_now())
            record["status"] = models.derive_status(record)
            self._sync_and_persist(previous_state)
            return self._serialize_record(self._get_record(port), base_url, public_host)

    def get_settings(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.state["server"])

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if not isinstance(payload, dict):
                raise ValueError("settings payload must be an object")
            previous_state = deepcopy(self.state)
            current = dict(self.state["server"])
            updated = apply_server_settings_patch(
                current,
                payload,
                self.server_settings_defaults,
            )
            self.state["server"] = updated
            self._apply_server_settings()
            try:
                self._sync_and_persist(previous_state)
            except Exception:
                self.state = deepcopy(previous_state)
                self._apply_server_settings()
                raise
            return dict(self.state["server"])

    def resolve_public_host(self, fallback_host: str) -> str:
        with self.lock:
            configured_host = str(self.state["server"].get("public_v2ray_host", "")).strip()
        return configured_host or fallback_host

    def delete_port(self, port: int) -> None:
        with self.lock:
            previous_state = deepcopy(self.state)
            self._get_record(port)
            self.state["ports"] = [item for item in self.state["ports"] if int(item["port"]) != port]
            self._sync_and_persist(previous_state)

    def reset_traffic(self, port: int, base_url: str, public_host: str) -> dict[str, Any]:
        with self.lock:
            previous_state = deepcopy(self.state)
            record = self._get_record(port)
            totals = self.traffic_client.get_port_totals([record])
            if port not in totals:
                raise RuntimeSyncError(f"traffic total not found for port {port}")
            total_bytes = totals[port]
            record["traffic_reset_base_bytes"] = total_bytes
            record["traffic_used_bytes"] = 0
            record["last_sync_error"] = ""
            record["updated_at"] = models.format_timestamp(models.utc_now())
            record["status"] = models.derive_status(record)
            self._sync_and_persist(previous_state)
            return self._serialize_record(self._get_record(port), base_url, public_host)

    def sync_port(self, port: int, base_url: str, public_host: str) -> dict[str, Any]:
        with self.lock:
            previous_state = deepcopy(self.state)
            self._get_record(port)
            try:
                self.sync_all_locked()
                self._sync_nginx_config()
                self._persist_state()
            except Exception:
                self.state = deepcopy(previous_state)
                self._apply_server_settings()
                raise
            record = self._get_record(port)
            return self._serialize_record(record, base_url, public_host)

    def sync_all(self, force_runtime_reset: bool = False) -> None:
        with self.lock:
            previous_state = deepcopy(self.state)
            try:
                self.sync_all_locked(force_runtime_reset=force_runtime_reset)
                self._sync_nginx_config()
                self._persist_state()
            except Exception:
                self.state = deepcopy(previous_state)
                self._apply_server_settings()
                raise

    def sync_all_locked(self, force_runtime_reset: bool = False) -> None:
        now = models.utc_now()
        records = self.state["ports"]

        totals: dict[int, int] = {}
        stats_error = ""
        try:
            totals = self.traffic_client.get_port_totals(records)
        except RuntimeSyncError as exc:
            stats_error = str(exc)

        for record in records:
            port = int(record["port"])
            sync_time = models.format_timestamp(now)
            current_total = totals.get(port)
            if current_total is not None:
                record["traffic_used_bytes"] = max(0, current_total - int(record["traffic_reset_base_bytes"]))

            base_status = models.derive_rule_status(record, now)
            error_message = stats_error

            record["last_synced_at"] = sync_time
            record["last_sync_error"] = error_message
            record["status"] = (
                models.STATUS_SYNC_ERROR
                if base_status == models.STATUS_ACTIVE and error_message
                else base_status
            )

    def _persist_state(self) -> None:
        self.state = self.store.save(self.state)

    def _sync_and_persist(self, previous_state: dict[str, Any]) -> None:
        try:
            self.sync_all_locked()
            self._sync_nginx_config()
            self._persist_state()
        except Exception:
            self.state = deepcopy(previous_state)
            self._apply_server_settings()
            raise

    def _ensure_server_settings(self) -> None:
        normalized = normalize_server_settings(
            self.state.get("server", {}),
            self.server_settings_defaults,
        )
        if self.state.get("server") != normalized:
            self.state["server"] = normalized
            self._persist_state()

    def _apply_server_settings(self) -> None:
        settings = self.state["server"]
        self.tls_enabled = bool(settings["public_tls"])
        if hasattr(self.runtime_client, "server"):
            self.runtime_client.server = str(settings["v2ray_api_server"])
        if self.traffic_client_factory is not None:
            self.traffic_client = self.traffic_client_factory(self.runtime_client, settings)
        if self.nginx_port_manager is not None:
            self.nginx_port_manager.configure(
                output_path=settings["nginx_config_output_path"],
                reload_command=settings["nginx_reload_command"],
                upstream_host=settings["fixed_v2ray_upstream_host"],
                upstream_port=int(settings["fixed_v2ray_upstream_port"]),
            )

    def _sync_nginx_config(self) -> None:
        if self.nginx_port_manager is None:
            return
        active_records = [
            record
            for record in self.state["ports"]
            if models.derive_rule_status(record) == models.STATUS_ACTIVE
        ]
        self.nginx_port_manager.sync(active_records)

    def _get_record(self, port: int) -> dict[str, Any]:
        for record in self.state["ports"]:
            if int(record["port"]) == int(port):
                return record
        raise KeyError(f"port not found: {port}")

    def _get_record_by_token(self, token: str) -> dict[str, Any]:
        for record in self.state["ports"]:
            if record["subscription_token"] == token:
                return record
        raise KeyError(f"subscription token not found: {token}")

    def _validate_new_record(self, record: dict[str, Any]) -> None:
        port = int(record["port"])
        if port < self.port_range_start or port > self.port_range_end:
            raise ValueError(
                f"port must be between {self.port_range_start} and {self.port_range_end}",
            )
        if port in self.reserved_ports:
            raise ValueError(f"port is reserved: {port}")
        if record["traffic_limit_bytes"] <= 0:
            raise ValueError("traffic_limit_bytes must be positive")
        if record["expires_at"]:
            models.parse_timestamp(record["expires_at"])

        for existing in self.state["ports"]:
            if int(existing["port"]) == port:
                raise ValueError(f"port already exists: {port}")
            if existing["uuid"] == record["uuid"]:
                raise ValueError(f"uuid already exists: {record['uuid']}")
            if existing["ws_path"] == record["ws_path"]:
                raise ValueError(f"ws_path already exists: {record['ws_path']}")
            if existing["subscription_token"] == record["subscription_token"]:
                raise ValueError("subscription_token already exists")

    def _serialize_record(self, record: dict[str, Any], base_url: str, public_host: str) -> dict[str, Any]:
        payload = build_vmess_payload(record, public_host, self.tls_enabled)
        vmess_link = build_vmess_link(payload)
        v2ray_path = f"/subscriptions/{record['subscription_token']}/v2ray"
        clash_path = f"/subscriptions/{record['subscription_token']}/clash"
        links_path = f"/links/{record['subscription_token']}"
        return {
            **record,
            "vmess_link": vmess_link,
            "rule_status": models.derive_rule_status(record),
            "traffic_remaining_bytes": max(
                0,
                int(record["traffic_limit_bytes"]) - int(record["traffic_used_bytes"]),
            ),
            "links": {
                "info": f"{base_url}{links_path}",
                "v2ray": f"{base_url}{v2ray_path}",
                "clash": f"{base_url}{clash_path}",
            },
        }
