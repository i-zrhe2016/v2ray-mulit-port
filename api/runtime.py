from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Iterable

from .models import build_port_email, build_port_tag


class RuntimeSyncError(RuntimeError):
    pass


def _parse_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return int(value.strip() or "0")
    raise ValueError(f"unsupported integer value: {value!r}")


class ShellV2RayRuntime:
    def __init__(
        self,
        command: str,
        server: str,
        timeout_seconds: int = 10,
        enabled: bool = True,
    ) -> None:
        self.command = command
        self.server = server
        self.timeout_seconds = timeout_seconds
        self.enabled = enabled

    def get_runtime_uptime(self) -> int:
        if not self.enabled:
            return 0
        output = self._run("api", "stats", f"--server={self.server}", "-json", "-runtime")
        data = json.loads(output or "{}")
        return _parse_int(data.get("Uptime", 0))

    def get_port_totals(self, records: Iterable[dict]) -> dict[int, int]:
        totals: dict[int, int] = {}
        names: list[str] = []
        name_to_port: dict[str, int] = {}

        for record in records:
            port = int(record["port"])
            uplink = f"user>>>{build_port_email(port)}>>>traffic>>>uplink"
            downlink = f"user>>>{build_port_email(port)}>>>traffic>>>downlink"
            names.extend([uplink, downlink])
            name_to_port[uplink] = port
            name_to_port[downlink] = port
            totals.setdefault(port, 0)

        if not names or not self.enabled:
            return totals

        output = self._run("api", "stats", f"--server={self.server}", "-json", *names)
        data = json.loads(output or "{}")
        for stat in data.get("stat", []):
            if not isinstance(stat, dict):
                continue
            port = name_to_port.get(str(stat.get("name", "")))
            if port is None:
                continue
            totals[port] = totals.get(port, 0) + _parse_int(stat.get("value", 0))

        return totals

    def add_inbound(self, record: dict) -> None:
        if not self.enabled:
            return

        config = {
            "inbounds": [
                {
                    "tag": build_port_tag(int(record["port"])),
                    "port": int(record["port"]),
                    "listen": "0.0.0.0",
                    "protocol": "vmess",
                    "settings": {
                        "clients": [
                            {
                                "id": record["uuid"],
                                "alterId": int(record.get("alter_id", 0)),
                                "email": build_port_email(int(record["port"])),
                            },
                        ],
                    },
                    "streamSettings": {
                        "network": "ws",
                        "wsSettings": {
                            "path": record["ws_path"],
                        },
                    },
                },
            ],
        }

        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                prefix="v2ray-inbound-",
                suffix=".json",
                delete=False,
            ) as handle:
                json.dump(config, handle, ensure_ascii=False)
                temp_path = handle.name
            self._run("api", "adi", f"--server={self.server}", temp_path)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

    def remove_inbound(self, record: dict) -> None:
        self.remove_inbound_by_tag(build_port_tag(int(record["port"])))

    def remove_inbound_by_tag(self, tag: str) -> None:
        if not self.enabled:
            return
        self._run("api", "rmi", f"--server={self.server}", "-tags", tag)

    def _run(self, *args: str) -> str:
        command = [self.command, *args]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise RuntimeSyncError(f"v2ray command not found: {self.command}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeSyncError(f"v2ray command timed out: {' '.join(command)}") from exc

        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "").strip()
            if not message:
                message = f"command failed with exit code {completed.returncode}"
            raise RuntimeSyncError(message)

        return completed.stdout.strip()
