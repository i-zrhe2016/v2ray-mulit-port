import json
import os
import tempfile
import unittest
from base64 import b64decode

from api import models
from api.runtime import NginxJsonTrafficSource, RuntimeSyncError
from api.server import build_request_base_url, build_traffic_client, resolve_request_host_with_port
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
        self.uptime = 100
        self.active_ports: set[int] = set()
        self.totals: dict[int, int] = {}

    def get_runtime_uptime(self) -> int:
        return self.uptime

    def get_port_totals(self, records):
        return {int(record["port"]): self.totals.get(int(record["port"]), 0) for record in records}

    def add_inbound(self, record) -> None:
        self.active_ports.add(int(record["port"]))

    def remove_inbound(self, record) -> None:
        self.active_ports.discard(int(record["port"]))


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


class PanelServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(os.path.join(self.temp_dir.name, "ports.json"))
        self.runtime = FakeRuntimeClient()
        self.service = PanelService(
            store=self.store,
            runtime_client=self.runtime,
            port_range_start=20000,
            port_range_end=20010,
            reserved_ports={2016, 10085},
            tls_enabled=False,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

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
        self.assertIn(20001, self.runtime.active_ports)
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
        self.assertNotIn(20001, self.runtime.active_ports)

        enabled = self.service.update_port(
            20001,
            {"enabled": True},
            "https://panel.example.com",
            "example.com",
        )
        self.assertEqual(enabled["status"], "active")
        self.assertIn(20001, self.runtime.active_ports)

        self.runtime.totals[20001] = 4096
        reset = self.service.reset_traffic(20001, "https://panel.example.com", "example.com")
        self.assertEqual(reset["traffic_used_bytes"], 0)

        self.service.delete_port(20001)
        self.assertEqual(self.service.list_ports("https://panel.example.com", "example.com"), [])

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
        self.assertIn(20001, self.runtime.active_ports)

        traffic.totals[20001] = 2048
        synced = service.sync_port(20001, "https://panel.example.com", "example.com")
        self.assertEqual(synced["traffic_used_bytes"], 2048)
        self.assertEqual(synced["status"], "active")
        self.assertIn(20001, self.runtime.active_ports)

        traffic.totals[20001] = 3072
        reset = service.reset_traffic(20001, "https://panel.example.com", "example.com")
        self.assertEqual(reset["traffic_used_bytes"], 0)
        self.assertEqual(reset["traffic_reset_base_bytes"], 3072)

        traffic.totals[20001] = 8000
        exhausted = service.sync_port(20001, "https://panel.example.com", "example.com")
        self.assertEqual(exhausted["traffic_used_bytes"], 4928)
        self.assertEqual(exhausted["status"], "exhausted")
        self.assertNotIn(20001, self.runtime.active_ports)

    def test_traffic_source_error_preserves_usage_and_surfaces_sync_error(self) -> None:
        traffic = FakeTrafficSource()
        service = PanelService(
            store=self.store,
            runtime_client=self.runtime,
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

    def test_missing_nginx_port_total_preserves_usage_and_surfaces_sync_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stats_path = os.path.join(temp_dir, "nginx-traffic.json")
            with open(stats_path, "w", encoding="utf-8") as handle:
                json.dump({"20001": 2048}, handle)

            service = PanelService(
                store=self.store,
                runtime_client=self.runtime,
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
