from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from typing import Iterable

from .models import build_port_email, build_port_tag


class RuntimeSyncError(RuntimeError):
    pass


def _invalid_nginx_total(port: int, value: object) -> RuntimeSyncError:
    return RuntimeSyncError(f"invalid nginx traffic total for port {port}: {value!r}")


def _parse_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return int(value.strip() or "0")
    raise ValueError(f"unsupported integer value: {value!r}")


def _parse_nginx_total(port: int, value: object) -> int:
    if isinstance(value, (bool, float)):
        raise _invalid_nginx_total(port, value)
    if isinstance(value, int):
        total = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise _invalid_nginx_total(port, value)
        try:
            total = int(raw)
        except ValueError as exc:
            raise _invalid_nginx_total(port, value) from exc
    else:
        raise _invalid_nginx_total(port, value)

    if total < 0:
        raise RuntimeSyncError(f"nginx traffic total must be non-negative for port {port}")
    return total


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


class NginxJsonTrafficSource:
    def __init__(self, path: str) -> None:
        self.path = path

    def get_port_totals(self, records: Iterable[dict]) -> dict[int, int]:
        ports = {int(record["port"]) for record in records}
        if not ports:
            return {}

        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError as exc:
            raise RuntimeSyncError(f"nginx traffic stats file not found: {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeSyncError(f"invalid nginx traffic stats JSON: {exc.msg}") from exc
        except OSError as exc:
            raise RuntimeSyncError(f"failed to read nginx traffic stats file: {exc}") from exc

        if not isinstance(data, dict):
            raise RuntimeSyncError("nginx traffic stats must be a JSON object keyed by port")

        totals: dict[int, int] = {}
        missing_ports: list[int] = []
        for port in ports:
            key = str(port)
            if key not in data:
                missing_ports.append(port)
                continue
            totals[port] = _parse_nginx_total(port, data[key])

        if missing_ports:
            missing = ", ".join(str(port) for port in sorted(missing_ports))
            raise RuntimeSyncError(f"nginx traffic total not found for port(s): {missing}")

        return totals


class NginxPortManager:
    def __init__(
        self,
        output_path: str,
        reload_command: str,
        upstream_host: str,
        upstream_port: int,
        timeout_seconds: int = 10,
    ) -> None:
        self.output_path = output_path
        self.reload_command = reload_command
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.timeout_seconds = timeout_seconds

    def configure(
        self,
        output_path: str,
        reload_command: str,
        upstream_host: str,
        upstream_port: int,
    ) -> None:
        self.output_path = output_path
        self.reload_command = reload_command
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port

    def render(self, records: Iterable[dict]) -> str:
        active_records = sorted(records, key=lambda item: int(item["port"]))
        lines = [
            "# Managed by v2ray-panel. Include this file from an nginx http context.",
        ]
        for record in active_records:
            port = int(record["port"])
            path = str(record.get("ws_path", "/")).strip() or "/"
            lines.extend(
                [
                    "server {",
                    f"    listen {port};",
                    "    location / {",
                    "        return 404;",
                    "    }",
                    f"    location = {path} {{",
                    f"        proxy_pass http://{self.upstream_host}:{self.upstream_port};",
                    "        proxy_http_version 1.1;",
                    "        proxy_set_header Upgrade $http_upgrade;",
                    '        proxy_set_header Connection "upgrade";',
                    "        proxy_set_header Host $host;",
                    "    }",
                    "}",
                ],
            )
        lines.append("")
        return "\n".join(lines)

    def write_config(self, content: str) -> None:
        directory = os.path.dirname(self.output_path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".nginx-managed.",
            suffix=".conf",
            dir=directory,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(temp_path, self.output_path)
        except OSError as exc:
            raise RuntimeSyncError(f"failed to write nginx config: {exc}") from exc
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def reload(self) -> None:
        command = shlex.split(self.reload_command)
        if not command:
            raise RuntimeSyncError("nginx reload command is required")
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise RuntimeSyncError(f"nginx reload command not found: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeSyncError(f"nginx reload timed out: {' '.join(command)}") from exc

        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "").strip()
            if not message:
                message = f"nginx reload failed with exit code {completed.returncode}"
            raise RuntimeSyncError(message)

    def sync(self, records: Iterable[dict]) -> None:
        content = self.render(records)
        self.write_config(content)
        self.reload()
