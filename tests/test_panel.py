import json
import os
import tempfile
import threading
import urllib.request
import unittest
from base64 import b64decode

from api import models
from api.runtime import NginxJsonTrafficSource, NginxPortManager, RuntimeSyncError
from api.server import Handler, PanelHTTPServer, build_request_base_url, build_traffic_client, resolve_request_host_with_port
from api.service import PanelService
from api.store import StateStore, StateValidationError, validate_state
from api.subscriptions import (
    SUBSCRIPTION_VARIANT_BASE_NAME,
    build_clash_converter_url,
    build_v2ray_subscription_body,
    build_vmess_link,
    build_vmess_payload,
    build_vmess_payload_variants,
)


class FakeRuntimeClient:
    def __init__(self) -> None:
        self.server = "127.0.0.1:10085"
        self.uptime = 100
        self.totals: dict[int, int] = {}

    def get_runtime_uptime(self) -> int:
        return self.uptime

    def get_port_totals(self, records):
        return {int(record["port"]): self.totals.get(int(record["port"]), 0) for record in records}

class FakeNginxPortManager:
    def __init__(self) -> None:
        self.output_path = "/tmp/nginx-managed.conf"
        self.reload_command = "nginx -s reload"
        self.upstream_host = "127.0.0.1"
        self.upstream_port = 10085
        self.synced_ports: list[list[int]] = []

    def configure(self, output_path: str, reload_command: str, upstream_host: str, upstream_port: int) -> None:
        self.output_path = output_path
        self.reload_command = reload_command
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port

    def sync(self, records) -> None:
        self.synced_ports.append(sorted(int(record["port"]) for record in records))


class FailingNginxPortManager(FakeNginxPortManager):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def sync(self, records) -> None:
        raise RuntimeSyncError(self.message)


class FakeTrafficSource:
    def __init__(self) -> None:
        self.totals: dict[int, int] = {}

    def get_port_totals(self, records):
        return {
            int(record["port"]): self.totals[int(record["port"])]
            for record in records
            if int(record["port"]) in self.totals
        }


class FailingTrafficSource:
    def __init__(self, message: str) -> None:
        self.message = message

    def get_port_totals(self, records):
        raise RuntimeSyncError(self.message)


def decode_vmess_link(link: str) -> dict:
    return json.loads(b64decode(link.removeprefix("vmess://")).decode("utf-8"))


def make_server_defaults(**overrides) -> dict:
    settings = {
        "v2ray_api_server": "127.0.0.1:10085",
        "fixed_v2ray_upstream_host": "127.0.0.1",
        "fixed_v2ray_upstream_port": 10085,
        "public_v2ray_host": "default.example.com",
        "public_tls": False,
        "traffic_stats_source": "v2ray",
        "nginx_stats_json_path": "/data/nginx-traffic.json",
        "nginx_config_output_path": "/data/nginx-managed-http.conf",
        "nginx_reload_command": "nginx -s reload",
    }
    settings.update(overrides)
    return settings


class SubscriptionTests(unittest.TestCase):
    def test_build_clash_converter_url(self) -> None:
        link = build_clash_converter_url(
            "https://panel.example.com/subscriptions/token/v2ray",
            "http://subconverter:25500/sub",
            "https://example.com/template.ini",
        )
        self.assertIn("target=clash", link)
        self.assertIn("url=https%3A%2F%2Fpanel.example.com%2Fsubscriptions%2Ftoken%2Fv2ray", link)
        self.assertIn("config=https%3A%2F%2Fexample.com%2Ftemplate.ini", link)

    def test_build_single_port_v2ray_subscription(self) -> None:
        payload = build_vmess_payload(
            {
                "remark": "user-20001",
                "port": 20001,
                "uuid": "11111111-1111-1111-1111-111111111111",
                "alter_id": 0,
                "ws_path": "/ws/20001",
            },
            "example.com",
            True,
        )
        link = build_vmess_link(payload)
        body = build_v2ray_subscription_body(link)
        self.assertTrue(link.startswith("vmess://"))
        self.assertTrue(body)

    def test_build_multi_variant_v2ray_subscription(self) -> None:
        payloads = build_vmess_payload_variants(
            {
                "remark": "user-20001",
                "port": 20001,
                "uuid": "11111111-1111-1111-1111-111111111111",
                "alter_id": 0,
                "ws_path": "/ws/20001",
            },
            "example.com",
            True,
            6,
        )
        self.assertEqual(len(payloads), 6)
        self.assertEqual(payloads[0]["ps"], f"{SUBSCRIPTION_VARIANT_BASE_NAME} 01")
        self.assertEqual(payloads[-1]["ps"], f"{SUBSCRIPTION_VARIANT_BASE_NAME} 06")

        links = [build_vmess_link(payload) for payload in payloads]
        body = build_v2ray_subscription_body(links)
        decoded = b64decode(body).decode("utf-8").strip().splitlines()
        self.assertEqual(len(decoded), 6)
        self.assertTrue(all(line.startswith("vmess://") for line in decoded))


class RequestUrlResolutionTests(unittest.TestCase):
    def test_localhost_request_uses_public_host_for_base_url(self) -> None:
        original_public_host = os.environ.get("V2RAY_PUBLIC_HOST")
        original_api_port = os.environ.get("API_PORT")
        try:
            os.environ["V2RAY_PUBLIC_HOST"] = "206.189.148.251"
            os.environ["API_PORT"] = "2016"
            headers = {"Host": "127.0.0.1:2016"}
            self.assertEqual(resolve_request_host_with_port(headers), "206.189.148.251:2016")
            self.assertEqual(build_request_base_url(headers), "http://206.189.148.251:2016")
        finally:
            if original_public_host is None:
                os.environ.pop("V2RAY_PUBLIC_HOST", None)
            else:
                os.environ["V2RAY_PUBLIC_HOST"] = original_public_host
            if original_api_port is None:
                os.environ.pop("API_PORT", None)
            else:
                os.environ["API_PORT"] = original_api_port

    def test_configured_public_base_url_wins(self) -> None:
        original_public_base = os.environ.get("PANEL_PUBLIC_BASE_URL")
        try:
            os.environ["PANEL_PUBLIC_BASE_URL"] = "https://panel.example.com"
            headers = {"Host": "127.0.0.1:2016"}
            self.assertEqual(resolve_request_host_with_port(headers), "panel.example.com")
            self.assertEqual(build_request_base_url(headers), "https://panel.example.com")
        finally:
            if original_public_base is None:
                os.environ.pop("PANEL_PUBLIC_BASE_URL", None)
            else:
                os.environ["PANEL_PUBLIC_BASE_URL"] = original_public_base


class TrafficSourceConfigTests(unittest.TestCase):
    def test_build_traffic_client_uses_runtime_by_default_and_nginx_when_configured(self) -> None:
        original_source = os.environ.get("TRAFFIC_STATS_SOURCE")
        original_path = os.environ.get("NGINX_TRAFFIC_STATS_FILE")
        try:
            os.environ.pop("TRAFFIC_STATS_SOURCE", None)
            os.environ.pop("NGINX_TRAFFIC_STATS_FILE", None)
            runtime = FakeRuntimeClient()
            self.assertIs(build_traffic_client(runtime), runtime)

            os.environ["TRAFFIC_STATS_SOURCE"] = "nginx_json"
            os.environ["NGINX_TRAFFIC_STATS_FILE"] = "/tmp/nginx-traffic.json"
            source = build_traffic_client(runtime)
            self.assertIsInstance(source, NginxJsonTrafficSource)
            self.assertEqual(source.path, "/tmp/nginx-traffic.json")
        finally:
            if original_source is None:
                os.environ.pop("TRAFFIC_STATS_SOURCE", None)
            else:
                os.environ["TRAFFIC_STATS_SOURCE"] = original_source
            if original_path is None:
                os.environ.pop("NGINX_TRAFFIC_STATS_FILE", None)
            else:
                os.environ["NGINX_TRAFFIC_STATS_FILE"] = original_path


class SettingsApiTests(unittest.TestCase):
    def test_get_and_patch_settings_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = PanelService(
                store=StateStore(os.path.join(temp_dir, "ports.json")),
                runtime_client=FakeRuntimeClient(),
                nginx_port_manager=FakeNginxPortManager(),
                port_range_start=20000,
                port_range_end=20010,
                reserved_ports={2016, 10085},
                server_settings_defaults=make_server_defaults(),
            )
            httpd = PanelHTTPServer(("127.0.0.1", 0), Handler, service, sync_interval=999)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{httpd.server_address[1]}"

            try:
                with urllib.request.urlopen(f"{base_url}/api/settings", timeout=5) as response:
                    settings = json.loads(response.read().decode("utf-8"))
                self.assertEqual(settings["v2ray_api_server"], "127.0.0.1:10085")
                self.assertEqual(settings["fixed_v2ray_upstream_host"], "127.0.0.1")
                self.assertEqual(settings["fixed_v2ray_upstream_port"], 10085)

                request = urllib.request.Request(
                    f"{base_url}/api/settings",
                    method="PATCH",
                    data=json.dumps(
                        {
                            "v2ray_api_server": "10.0.0.8:10090",
                            "fixed_v2ray_upstream_host": "10.0.0.5",
                            "fixed_v2ray_upstream_port": 10086,
                            "public_v2ray_host": "saved.example.com",
                            "public_tls": True,
                            "nginx_config_output_path": "/tmp/panel-nginx.conf",
                            "nginx_reload_command": "nginx -s reload",
                        },
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    updated = json.loads(response.read().decode("utf-8"))

                self.assertEqual(updated["v2ray_api_server"], "10.0.0.8:10090")
                self.assertEqual(updated["fixed_v2ray_upstream_host"], "10.0.0.5")
                self.assertEqual(updated["fixed_v2ray_upstream_port"], 10086)
                self.assertEqual(updated["public_v2ray_host"], "saved.example.com")
                self.assertTrue(updated["public_tls"])
                self.assertEqual(service.get_settings()["public_v2ray_host"], "saved.example.com")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)

    def test_patch_settings_returns_runtime_sync_error_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = PanelService(
                store=StateStore(os.path.join(temp_dir, "ports.json")),
                runtime_client=FakeRuntimeClient(),
                nginx_port_manager=FailingNginxPortManager("reload failed"),
                port_range_start=20000,
                port_range_end=20010,
                reserved_ports={2016, 10085},
                server_settings_defaults=make_server_defaults(),
            )
            httpd = PanelHTTPServer(("127.0.0.1", 0), Handler, service, sync_interval=999)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{httpd.server_address[1]}"

            try:
                request = urllib.request.Request(
                    f"{base_url}/api/settings",
                    method="PATCH",
                    data=json.dumps({"fixed_v2ray_upstream_host": "10.0.0.9"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(request, timeout=5)

                self.assertEqual(ctx.exception.code, 500)
                body = json.loads(ctx.exception.read().decode("utf-8"))
                self.assertEqual(body["error"], "reload failed")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)


class StateStoreTests(unittest.TestCase):
    def test_validate_state_rejects_duplicate_ports(self) -> None:
        with self.assertRaises(StateValidationError):
            validate_state(
                {
                    "version": 1,
                    "server": {},
                    "ports": [
                        models.make_port_record({"port": 20001, "traffic_limit_bytes": 1}),
                        models.make_port_record({"port": 20001, "traffic_limit_bytes": 1}),
                    ],
                },
            )

    def test_state_store_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(os.path.join(temp_dir, "ports.json"))
            state = models.fresh_state()
            state["ports"].append(models.make_port_record({"port": 20001, "traffic_limit_bytes": 1024}))
            store.save(state)
            loaded = store.load()
            self.assertEqual(len(loaded["ports"]), 1)
            self.assertEqual(loaded["ports"][0]["port"], 20001)


class NginxJsonTrafficSourceTests(unittest.TestCase):
    def test_reads_cumulative_totals_by_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stats_path = os.path.join(temp_dir, "nginx-traffic.json")
            with open(stats_path, "w", encoding="utf-8") as handle:
                json.dump({"20001": 123456789, "20002": "987654321"}, handle)

            source = NginxJsonTrafficSource(stats_path)
            totals = source.get_port_totals(
                [
                    {"port": 20001},
                    {"port": 20002},
                ],
            )

            self.assertEqual(totals, {20001: 123456789, 20002: 987654321})

    def test_missing_or_invalid_file_raises_runtime_sync_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = NginxJsonTrafficSource(os.path.join(temp_dir, "missing.json"))
            with self.assertRaises(RuntimeSyncError):
                source.get_port_totals([{"port": 20001}])

            stats_path = os.path.join(temp_dir, "nginx-traffic.json")
            with open(stats_path, "w", encoding="utf-8") as handle:
                handle.write("{invalid")
            source = NginxJsonTrafficSource(stats_path)
            with self.assertRaises(RuntimeSyncError):
                source.get_port_totals([{"port": 20001}])

    def test_missing_port_total_raises_runtime_sync_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stats_path = os.path.join(temp_dir, "nginx-traffic.json")
            with open(stats_path, "w", encoding="utf-8") as handle:
                json.dump({"20001": 123456789}, handle)

            source = NginxJsonTrafficSource(stats_path)
            with self.assertRaises(RuntimeSyncError):
                source.get_port_totals([{"port": 20001}, {"port": 20002}])

    def test_invalid_port_total_raises_runtime_sync_error(self) -> None:
        invalid_values = [-1, True, 1.5, ""]
        for value in invalid_values:
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as temp_dir:
                    stats_path = os.path.join(temp_dir, "nginx-traffic.json")
                    with open(stats_path, "w", encoding="utf-8") as handle:
                        json.dump({"20001": value}, handle)

                    source = NginxJsonTrafficSource(stats_path)
                    with self.assertRaises(RuntimeSyncError):
                        source.get_port_totals([{"port": 20001}])


class NginxPortManagerTests(unittest.TestCase):
    def test_render_outputs_single_upstream_servers(self) -> None:
        manager = NginxPortManager(
            output_path="/tmp/managed.conf",
            reload_command="nginx -s reload",
            upstream_host="127.0.0.1",
            upstream_port=10085,
        )
        content = manager.render(
            [
                {"port": 20002},
                {"port": 20001},
            ],
        )
        self.assertIn("listen 20001;", content)
        self.assertIn("listen 20002;", content)
        self.assertIn("proxy_pass http://127.0.0.1:10085;", content)

    def test_sync_writes_file_and_executes_reload_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "managed.conf")
            marker_path = os.path.join(temp_dir, "reloaded.txt")
            manager = NginxPortManager(
                output_path=output_path,
                reload_command=f"sh -c 'echo ok > {marker_path}'",
                upstream_host="127.0.0.1",
                upstream_port=10085,
            )
            manager.sync([{"port": 20001}])

            with open(output_path, "r", encoding="utf-8") as handle:
                content = handle.read()
            self.assertIn("listen 20001;", content)
            self.assertTrue(os.path.exists(marker_path))

    def test_reload_failure_raises_runtime_sync_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = NginxPortManager(
                output_path=os.path.join(temp_dir, "managed.conf"),
                reload_command="sh -c 'exit 2'",
                upstream_host="127.0.0.1",
                upstream_port=10085,
            )
            with self.assertRaises(RuntimeSyncError):
                manager.sync([{"port": 20001}])


class PanelServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(os.path.join(self.temp_dir.name, "ports.json"))
        self.runtime = FakeRuntimeClient()
        self.nginx_manager = FakeNginxPortManager()
        self.service = PanelService(
            store=self.store,
            runtime_client=self.runtime,
            nginx_port_manager=self.nginx_manager,
            port_range_start=20000,
            port_range_end=20010,
            reserved_ports={2016, 10085},
            tls_enabled=False,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_server_settings_defaults_are_persisted_and_drive_runtime_and_subscriptions(self) -> None:
        store = StateStore(os.path.join(self.temp_dir.name, "settings-ports.json"))
        runtime = FakeRuntimeClient()
        service = PanelService(
            store=store,
            runtime_client=runtime,
            nginx_port_manager=FakeNginxPortManager(),
            port_range_start=20000,
            port_range_end=20010,
            reserved_ports={2016, 10085},
            server_settings_defaults=make_server_defaults(public_v2ray_host="default.example.com"),
        )

        self.assertEqual(service.get_settings()["public_v2ray_host"], "default.example.com")
        self.assertEqual(store.load()["server"]["public_v2ray_host"], "default.example.com")
        self.assertEqual(runtime.server, "127.0.0.1:10085")

        created = service.create_port(
            {"port": 20001, "traffic_limit_bytes": 4096, "expires_at": models.default_expires_at()},
            "https://panel.example.com",
            service.resolve_public_host("request.example.com"),
        )
        payload = decode_vmess_link(created["vmess_link"])
        self.assertEqual(payload["add"], "default.example.com")
        self.assertEqual(payload["tls"], "")

        service.update_settings(
            {
                "v2ray_api_server": "10.0.0.8:10090",
                "fixed_v2ray_upstream_host": "10.0.0.5",
                "fixed_v2ray_upstream_port": 10086,
                "public_v2ray_host": "saved.example.com",
                "public_tls": True,
            },
        )

        listed = service.list_ports(
            "https://panel.example.com",
            service.resolve_public_host("request.example.com"),
        )[0]
        updated_payload = decode_vmess_link(listed["vmess_link"])
        self.assertEqual(updated_payload["add"], "saved.example.com")
        self.assertEqual(updated_payload["tls"], "tls")
        self.assertEqual(runtime.server, "10.0.0.8:10090")
        self.assertEqual(service.nginx_port_manager.upstream_host, "10.0.0.5")
        self.assertEqual(service.nginx_port_manager.upstream_port, 10086)

    def test_settings_validation_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            self.service.update_settings({"traffic_stats_source": "unknown"})

        with self.assertRaises(ValueError):
            self.service.update_settings({"v2ray_api_server": ""})

        with self.assertRaises(ValueError):
            self.service.update_settings({"fixed_v2ray_upstream_host": ""})

        with self.assertRaises(ValueError):
            self.service.update_settings({"fixed_v2ray_upstream_port": 0})

        with self.assertRaises(ValueError):
            self.service.update_settings(
                {
                    "traffic_stats_source": "nginx_json",
                    "nginx_stats_json_path": "",
                },
            )

    def test_saved_traffic_source_switches_subsequent_sync_reads(self) -> None:
        stats_path = os.path.join(self.temp_dir.name, "nginx-traffic.json")
        with open(stats_path, "w", encoding="utf-8") as handle:
            json.dump({"20001": 3072}, handle)

        store = StateStore(os.path.join(self.temp_dir.name, "traffic-settings-ports.json"))
        runtime = FakeRuntimeClient()
        service = PanelService(
            store=store,
            runtime_client=runtime,
            nginx_port_manager=FakeNginxPortManager(),
            traffic_client_factory=build_traffic_client,
            port_range_start=20000,
            port_range_end=20010,
            reserved_ports={2016, 10085},
            server_settings_defaults=make_server_defaults(public_v2ray_host="example.com"),
        )
        service.create_port(
            {"port": 20001, "traffic_limit_bytes": 4096, "expires_at": models.default_expires_at()},
            "https://panel.example.com",
            "example.com",
        )

        service.update_settings(
            {
                "traffic_stats_source": "nginx_json",
                "nginx_stats_json_path": stats_path,
            },
        )
        synced = service.list_ports("https://panel.example.com", "example.com")[0]

        self.assertIsInstance(service.traffic_client, NginxJsonTrafficSource)
        self.assertEqual(service.traffic_client.path, stats_path)
        self.assertEqual(synced["traffic_used_bytes"], 3072)

    def test_add_update_disable_reset_and_delete_port(self) -> None:
        created = self.service.create_port(
            {
                "port": 20001,
                "remark": "alice",
                "traffic_limit_bytes": 10 * 1024,
                "expires_at": models.default_expires_at(),
            },
            "https://panel.example.com",
            "example.com",
        )
        self.assertEqual(created["port"], 20001)
        self.assertEqual(self.nginx_manager.synced_ports[-1], [20001])
        self.assertTrue(created["links"]["v2ray"].endswith("/subscriptions/" + created["subscription_token"] + "/v2ray"))

        self.runtime.totals[20001] = 2048
        updated = self.service.update_port(
            20001,
            {"traffic_limit_bytes": 20 * 1024, "remark": "alice-new"},
            "https://panel.example.com",
            "example.com",
        )
        self.assertEqual(updated["remark"], "alice-new")
        self.assertEqual(updated["traffic_used_bytes"], 2048)

        disabled = self.service.update_port(
            20001,
            {"enabled": False},
            "https://panel.example.com",
            "example.com",
        )
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(self.nginx_manager.synced_ports[-1], [])

        enabled = self.service.update_port(
            20001,
            {"enabled": True},
            "https://panel.example.com",
            "example.com",
        )
        self.assertEqual(enabled["status"], "active")
        self.assertEqual(self.nginx_manager.synced_ports[-1], [20001])

        self.runtime.totals[20001] = 4096
        reset = self.service.reset_traffic(20001, "https://panel.example.com", "example.com")
        self.assertEqual(reset["traffic_used_bytes"], 0)

        self.service.delete_port(20001)
        self.assertEqual(self.service.list_ports("https://panel.example.com", "example.com"), [])
        self.assertEqual(self.nginx_manager.synced_ports[-1], [])

    def test_reject_duplicate_port(self) -> None:
        self.service.create_port(
            {"port": 20001, "traffic_limit_bytes": 1024, "expires_at": models.default_expires_at()},
            "https://panel.example.com",
            "example.com",
        )
        with self.assertRaises(ValueError):
            self.service.create_port(
                {"port": 20001, "traffic_limit_bytes": 2048, "expires_at": models.default_expires_at()},
                "https://panel.example.com",
                "example.com",
            )

    def test_port_outside_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.create_port(
                {"port": 19999, "traffic_limit_bytes": 1024, "expires_at": models.default_expires_at()},
                "https://panel.example.com",
                "example.com",
            )

    def test_separate_traffic_source_drives_usage_reset_and_exhaustion(self) -> None:
        traffic = FakeTrafficSource()
        service = PanelService(
            store=self.store,
            runtime_client=self.runtime,
            nginx_port_manager=self.nginx_manager,
            traffic_client=traffic,
            port_range_start=20000,
            port_range_end=20010,
            reserved_ports={2016, 10085},
            tls_enabled=False,
        )

        created = service.create_port(
            {"port": 20001, "traffic_limit_bytes": 4096, "expires_at": models.default_expires_at()},
            "https://panel.example.com",
            "example.com",
        )
        self.assertEqual(created["traffic_used_bytes"], 0)
        self.assertEqual(self.nginx_manager.synced_ports[-1], [20001])

        traffic.totals[20001] = 2048
        synced = service.sync_port(20001, "https://panel.example.com", "example.com")
        self.assertEqual(synced["traffic_used_bytes"], 2048)
        self.assertEqual(synced["status"], "active")
        self.assertEqual(self.nginx_manager.synced_ports[-1], [20001])

        traffic.totals[20001] = 3072
        reset = service.reset_traffic(20001, "https://panel.example.com", "example.com")
        self.assertEqual(reset["traffic_used_bytes"], 0)
        self.assertEqual(reset["traffic_reset_base_bytes"], 3072)

        traffic.totals[20001] = 8000
        exhausted = service.sync_port(20001, "https://panel.example.com", "example.com")
        self.assertEqual(exhausted["traffic_used_bytes"], 4928)
        self.assertEqual(exhausted["status"], "exhausted")
        self.assertEqual(self.nginx_manager.synced_ports[-1], [])

    def test_traffic_source_error_preserves_usage_and_surfaces_sync_error(self) -> None:
        traffic = FakeTrafficSource()
        service = PanelService(
            store=self.store,
            runtime_client=self.runtime,
            nginx_port_manager=FakeNginxPortManager(),
            traffic_client=traffic,
            port_range_start=20000,
            port_range_end=20010,
            reserved_ports={2016, 10085},
            tls_enabled=False,
        )
        service.create_port(
            {"port": 20001, "traffic_limit_bytes": 4096, "expires_at": models.default_expires_at()},
            "https://panel.example.com",
            "example.com",
        )
        traffic.totals[20001] = 2048
        service.sync_port(20001, "https://panel.example.com", "example.com")

        service.traffic_client = FailingTrafficSource("nginx stats unavailable")
        synced = service.sync_port(20001, "https://panel.example.com", "example.com")

        self.assertEqual(synced["traffic_used_bytes"], 2048)
        self.assertEqual(synced["status"], "sync_error")
        self.assertEqual(synced["last_sync_error"], "nginx stats unavailable")

    def test_nginx_reload_failure_does_not_persist_state_changes(self) -> None:
        service = PanelService(
            store=self.store,
            runtime_client=self.runtime,
            nginx_port_manager=FailingNginxPortManager("reload failed"),
            port_range_start=20000,
            port_range_end=20010,
            reserved_ports={2016, 10085},
            tls_enabled=False,
        )

        with self.assertRaises(RuntimeSyncError):
            service.create_port(
                {"port": 20001, "traffic_limit_bytes": 4096, "expires_at": models.default_expires_at()},
                "https://panel.example.com",
                "example.com",
            )

        self.assertEqual(service.list_ports("https://panel.example.com", "example.com"), [])
        self.assertEqual(self.store.load()["ports"], [])

    def test_missing_nginx_port_total_preserves_usage_and_surfaces_sync_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stats_path = os.path.join(temp_dir, "nginx-traffic.json")
            with open(stats_path, "w", encoding="utf-8") as handle:
                json.dump({"20001": 2048}, handle)

            service = PanelService(
                store=self.store,
                runtime_client=self.runtime,
                nginx_port_manager=FakeNginxPortManager(),
                traffic_client=NginxJsonTrafficSource(stats_path),
                port_range_start=20000,
                port_range_end=20010,
                reserved_ports={2016, 10085},
                tls_enabled=False,
            )
            service.create_port(
                {"port": 20001, "traffic_limit_bytes": 4096, "expires_at": models.default_expires_at()},
                "https://panel.example.com",
                "example.com",
            )
            synced = service.sync_port(20001, "https://panel.example.com", "example.com")
            self.assertEqual(synced["traffic_used_bytes"], 2048)

            with open(stats_path, "w", encoding="utf-8") as handle:
                json.dump({}, handle)

            stale = service.sync_port(20001, "https://panel.example.com", "example.com")
            self.assertEqual(stale["traffic_used_bytes"], 2048)
            self.assertEqual(stale["status"], "sync_error")
            self.assertIn("not found", stale["last_sync_error"])
