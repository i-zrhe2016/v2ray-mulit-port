import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from api import server


class ClashSubscriptionLinkTests(unittest.TestCase):
    def test_build_clash_subscription_link_with_default_template(self) -> None:
        source_subscription_url = "http://example.com:10086/"

        link = server.build_clash_subscription_link(
            source_subscription_url=source_subscription_url,
            converter_base_url="http://127.0.0.1:25500/sub",
        )

        parsed = urlparse(link)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(parsed.netloc, "127.0.0.1:25500")
        self.assertEqual(parsed.path, "/sub")
        self.assertEqual(query.get("target"), ["clash"])
        self.assertEqual(query.get("url"), [source_subscription_url])
        self.assertNotIn("config", query)

    def test_build_clash_subscription_link_with_config_template(self) -> None:
        source_subscription_url = "http://example.com:10086/"
        template_url = "https://example.com/default-template.ini"

        link = server.build_clash_subscription_link(
            source_subscription_url=source_subscription_url,
            converter_base_url="http://127.0.0.1:25500/sub",
            template_url=template_url,
        )

        query = parse_qs(urlparse(link).query)

        self.assertEqual(query.get("target"), ["clash"])
        self.assertEqual(query.get("url"), [source_subscription_url])
        self.assertEqual(query.get("config"), [template_url])


class RequestBaseUrlTests(unittest.TestCase):
    def test_build_request_base_url_prefers_forwarded_headers(self) -> None:
        headers = {
            "X-Forwarded-Host": "sub.example.com, internal",
            "X-Forwarded-Proto": "https",
            "Host": "127.0.0.1:10086",
        }

        self.assertEqual(server.build_request_base_url(headers), "https://sub.example.com/")

    def test_build_request_base_url_falls_back_to_host_header(self) -> None:
        headers = {"Host": "159.65.132.96:10086"}

        self.assertEqual(server.build_request_base_url(headers), "http://159.65.132.96:10086/")


class ConverterBaseUrlTests(unittest.TestCase):
    def test_resolve_converter_base_url_uses_request_host_by_default(self) -> None:
        headers = {"Host": "159.65.132.96:10086"}

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("V2RAY_SUBCONVERTER_URL", None)
            converter_url = server.resolve_converter_base_url(headers)

        self.assertEqual(converter_url, "http://159.65.132.96:10086/sub")


if __name__ == "__main__":
    unittest.main()
