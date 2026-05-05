from __future__ import annotations

import threading
from typing import Any

from . import models
from .runtime import RuntimeSyncError
from .store import StateStore
from .subscriptions import build_vmess_link, build_vmess_payload


class PanelService:
    def __init__(
        self,
        store: StateStore,
        runtime_client,
        port_range_start: int,
        port_range_end: int,
        reserved_ports: set[int] | None = None,
        traffic_client=None,
        tls_enabled: bool = False,
    ) -> None:
        self.store = store
        self.runtime_client = runtime_client
        self.traffic_client = traffic_client or runtime_client
        self.port_range_start = port_range_start
        self.port_range_end = port_range_end
        self.reserved_ports = reserved_ports or set()
        self.tls_enabled = tls_enabled
        self.lock = threading.RLock()
        self.state = self.store.load()
        self.runtime_applied_tags: set[str] = set()
        self.runtime_uptime: int | None = None

    def initialize(self) -> None:
        self.sync_all(force_runtime_reset=True)

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
            record = models.make_port_record(payload)
            self._validate_new_record(record)
            self.state["ports"].append(record)
            self.state["ports"].sort(key=lambda item: item["port"])
            self._persist_state()
            self.sync_all_locked()
            return self._serialize_record(self._get_record(record["port"]), base_url, public_host)

    def update_port(
        self,
        port: int,
        payload: dict[str, Any],
        base_url: str,
        public_host: str,
    ) -> dict[str, Any]:
        with self.lock:
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
            self._persist_state()
            self.sync_all_locked()
            return self._serialize_record(self._get_record(port), base_url, public_host)

    def delete_port(self, port: int) -> None:
        with self.lock:
            record = self._get_record(port)
            tag = models.build_port_tag(port)
            self.state["ports"] = [item for item in self.state["ports"] if int(item["port"]) != port]
            self._persist_state()
            try:
                self.runtime_client.remove_inbound(record)
            except RuntimeSyncError:
                pass
            self.runtime_applied_tags.discard(tag)

    def reset_traffic(self, port: int, base_url: str, public_host: str) -> dict[str, Any]:
        with self.lock:
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
            self._persist_state()
            self.sync_all_locked()
            return self._serialize_record(self._get_record(port), base_url, public_host)

    def sync_port(self, port: int, base_url: str, public_host: str) -> dict[str, Any]:
        with self.lock:
            self._get_record(port)
            self.sync_all_locked()
            record = self._get_record(port)
            return self._serialize_record(record, base_url, public_host)

    def sync_all(self, force_runtime_reset: bool = False) -> None:
        with self.lock:
            self.sync_all_locked(force_runtime_reset=force_runtime_reset)

    def sync_all_locked(self, force_runtime_reset: bool = False) -> None:
        now = models.utc_now()
        records = self.state["ports"]

        if force_runtime_reset:
            for record in records:
                try:
                    self.runtime_client.remove_inbound(record)
                except RuntimeSyncError:
                    pass
            self.runtime_applied_tags.clear()

        uptime_error = ""
        try:
            runtime_uptime = self.runtime_client.get_runtime_uptime()
            if self.runtime_uptime is not None and runtime_uptime < self.runtime_uptime:
                self.runtime_applied_tags.clear()
            self.runtime_uptime = runtime_uptime
        except RuntimeSyncError as exc:
            uptime_error = str(exc)

        totals: dict[int, int] = {}
        stats_error = ""
        try:
            totals = self.traffic_client.get_port_totals(records)
        except RuntimeSyncError as exc:
            stats_error = str(exc)

        for record in records:
            port = int(record["port"])
            tag = models.build_port_tag(port)
            sync_time = models.format_timestamp(now)
            current_total = totals.get(port)
            if current_total is not None:
                record["traffic_used_bytes"] = max(0, current_total - int(record["traffic_reset_base_bytes"]))

            base_status = models.derive_rule_status(record, now)
            error_message = uptime_error or stats_error

            if not uptime_error:
                if base_status == models.STATUS_ACTIVE and tag not in self.runtime_applied_tags:
                    try:
                        self.runtime_client.add_inbound(record)
                        self.runtime_applied_tags.add(tag)
                    except RuntimeSyncError as exc:
                        error_message = str(exc)
                elif base_status != models.STATUS_ACTIVE and tag in self.runtime_applied_tags:
                    try:
                        self.runtime_client.remove_inbound(record)
                        self.runtime_applied_tags.discard(tag)
                    except RuntimeSyncError as exc:
                        error_message = str(exc)

            record["last_synced_at"] = sync_time
            record["last_sync_error"] = error_message
            record["status"] = (
                models.STATUS_SYNC_ERROR
                if base_status == models.STATUS_ACTIVE and error_message
                else base_status
            )

        self._persist_state()

    def _persist_state(self) -> None:
        self.state = self.store.save(self.state)

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
