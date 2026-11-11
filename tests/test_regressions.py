"""Regression cases for fail-closed configuration and owned DNS mutations."""
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import sync


class RegressionTests(unittest.TestCase):
    def test_flag_rejects_unknown_values(self):
        with patch.dict(os.environ, {"TEST_FLAG": "sometimes"}, clear=False):
            with self.assertRaises(ValueError):
                sync.flag("TEST_FLAG")

    def test_managed_record_must_keep_marker_and_be_enabled(self):
        good = {"type": "A", "rData": {"ipAddress": "192.0.2.20"},
                "comments": sync.MARKER, "disabled": False}
        self.assertTrue(sync.can_change([good], "192.0.2.20"))
        for changes in ({"comments": "operator-owned"}, {"disabled": True}, {"comments": None}):
            record = dict(good)
            record.update(changes)
            self.assertFalse(sync.can_change([record], "192.0.2.20"))
        self.assertFalse(sync.can_change([good, dict(good)], "192.0.2.20"))
        self.assertFalse(sync.can_change([good], "192.0.2.21"))

    def _base_environment(self, **overrides):
        values = {
            "NPM_URL": "https://npm.internal",
            "NPM_IDENTITY": "reader@example.invalid",
            "NPM_SECRET": "dummy-npm-secret",
            "TECHNITIUM_URL": "https://dns.internal",
            "TECHNITIUM_TOKEN": "dummy-dns-token",
            "NPM_LAN_IP": "192.0.2.20",
            "DNS_ZONES": "apps.example.com",
        }
        values.update(overrides)
        return values

    def test_config_rejects_url_credentials_query_and_fragment(self):
        for key, value in (("NPM_URL", "https://user:pass@npm.internal"),
                           ("NPM_URL", "https://npm.internal/?token=leak"),
                           ("TECHNITIUM_URL", "https://dns.internal/#fragment")):
            with self.subTest(key=key, value=value), patch.dict(os.environ, self._base_environment(**{key: value}), clear=True):
                with self.assertRaises(ValueError):
                    sync.validate_config()

    def test_http_requires_explicit_opt_in(self):
        with patch.dict(os.environ, self._base_environment(NPM_URL="http://npm.internal"), clear=True):
            with self.assertRaises(ValueError):
                sync.validate_config()
        with patch.dict(os.environ, self._base_environment(
                NPM_URL="http://npm.internal", ALLOW_INSECURE_HTTP="true"), clear=True):
            self.assertEqual(sync.validate_config()["npm_url"], "http://npm.internal")

    def test_config_rejects_overlapping_zones_and_empty_credentials(self):
        with patch.dict(os.environ, self._base_environment(
                DNS_ZONES="example.com,apps.example.com"), clear=True):
            with self.assertRaises(ValueError):
                sync.validate_config()
        with patch.dict(os.environ, self._base_environment(NPM_SECRET="  "), clear=True):
            with self.assertRaises(ValueError):
                sync.validate_config()
        with patch.dict(os.environ, self._base_environment(TECHNITIUM_TOKEN=""), clear=True):
            with self.assertRaises(ValueError):
                sync.validate_config()

    def test_credentials_can_be_loaded_from_files_but_not_duplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            npm_file = pathlib.Path(directory) / "npm-secret"
            dns_file = pathlib.Path(directory) / "dns-token"
            npm_file.write_text("  file-npm-secret  \r\n", encoding="utf-8")
            dns_file.write_text(" file-dns-token \r\n", encoding="utf-8")
            env = self._base_environment(NPM_SECRET_FILE=str(npm_file),
                                        TECHNITIUM_TOKEN_FILE=str(dns_file))
            del env["NPM_SECRET"]
            del env["TECHNITIUM_TOKEN"]
            with patch.dict(os.environ, env, clear=True):
                config = sync.validate_config()
            self.assertEqual(config["npm_secret"], "  file-npm-secret  ")
            self.assertEqual(config["dns_token"], " file-dns-token ")
            with patch.dict(os.environ, dict(env, NPM_SECRET=""), clear=True):
                with self.assertRaises(ValueError):
                    sync.validate_config()
            with patch.dict(os.environ, dict(env, TECHNITIUM_TOKEN=""), clear=True):
                with self.assertRaises(ValueError):
                    sync.validate_config()

    def test_npm_hosts_fails_closed_on_malformed_enabled_and_domain_entries(self):
        config = {"npm_url": "https://npm.internal", "npm_identity": "reader",
                  "npm_secret": "dummy", "zones": ("apps.example.com",),
                  "allow_empty": False}
        responses = [
            [{"enabled": "false", "domain_names": ["app.apps.example.com"]}],
            [{"enabled": True, "domain_names": ["app.apps.example.com", None]}],
        ]
        for hosts in responses:
            def fake_api(method, url, params=None, payload=None, headers=None):
                if url.endswith("/api/tokens"):
                    return {"token": "dummy"}
                return hosts
            with self.subTest(hosts=hosts), patch.object(sync, "api", side_effect=fake_api):
                with self.assertRaises(RuntimeError):
                    sync.npm_hosts(config)

    def test_npm_hosts_accepts_boolean_or_integer_enabled_and_ignores_wildcards(self):
        config = {"npm_url": "https://npm.internal", "npm_identity": "reader",
                  "npm_secret": "dummy", "zones": ("apps.example.com",),
                  "allow_empty": False}
        hosts = [
            {"enabled": True, "domain_names": ["app.apps.example.com", "*.apps.example.com"]},
            {"enabled": 1, "domain_names": ["other.apps.example.com"]},
            {"enabled": False, "domain_names": ["disabled.apps.example.com"]},
            {"enabled": 0, "domain_names": ["also-disabled.apps.example.com"]},
        ]
        def fake_api(method, url, params=None, payload=None, headers=None):
            if url.endswith("/api/tokens"):
                return {"token": "dummy"}
            return hosts
        with patch.object(sync, "api", side_effect=fake_api):
            self.assertEqual(sync.npm_hosts(config), {
                "app.apps.example.com", "other.apps.example.com"})

    def test_state_rejects_malformed_owned_records_and_timestamps(self):
        invalid_states = [
            {"version": 1, "owned": {"bad_name.apps.example.com": "192.0.2.20"}, "stale_since": {}},
            {"version": 1, "owned": {"app.apps.example.com": "not-an-ip"}, "stale_since": {}},
            {"version": 1, "owned": {}, "stale_since": {"app.apps.example.com": float("nan")}},
            {"version": 1, "owned": {}, "stale_since": {"app.apps.example.com": -1}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "state.json"
            for state in invalid_states:
                path.write_text(json.dumps(state), encoding="utf-8")
                with self.subTest(state=state), self.assertRaises(RuntimeError):
                    sync.state_load(path)

    def test_technitium_requests_post_forms_and_never_put_tokens_in_urls(self):
        config = {"dns_url": "https://dns.internal", "dns_token": "dummy-token",
                  "tech_auth": "bearer"}

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, limit):
                return b'{"status":"ok"}'

        with patch.object(sync.urllib.request, "build_opener") as build_opener:
            build_opener.return_value.open.return_value = Response()
            sync.tech_call(config, "/api/zones/records/get", {"zone": "apps.example.com"})
        request = build_opener.return_value.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertNotIn("token", request.full_url.lower())
        self.assertEqual(request.get_header("Authorization"), "Bearer dummy-token")
        self.assertIn("zone=apps.example.com", request.data.decode("ascii"))

        config["tech_auth"] = "parameter"
        with patch.object(sync.urllib.request, "build_opener") as build_opener:
            build_opener.return_value.open.return_value = Response()
            sync.tech_call(config, "/api/zones/records/get", {"zone": "apps.example.com"})
        request = build_opener.return_value.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertNotIn("token", request.full_url.lower())
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIn("token=dummy-token", request.data.decode("ascii"))


if __name__ == "__main__":
    unittest.main()
