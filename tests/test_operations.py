"""Failure and boundary regressions for the reconciliation process."""
import copy
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

import sync
from test_sync import cfg


def managed(ip="192.0.2.20"):
    return {"type": "A", "disabled": False, "comments": sync.MARKER,
            "ttl": 120, "rData": {"ipAddress": ip}}


class OperationTests(unittest.TestCase):
    def test_duplicate_active_names_remain_desired_when_one_host_is_disabled(self):
        hosts = [{"enabled": True, "domain_names": ["app.apps.example.com", "alias.apps.example.com"]},
                 {"enabled": True, "domain_names": ["APP.apps.example.com."]},
                 {"enabled": False, "domain_names": ["app.apps.example.com"]}]
        with patch.object(sync, "api", side_effect=[{"token": "dummy"}, hosts]):
            self.assertEqual(sync.npm_hosts(cfg(Path("unused"))),
                             {"app.apps.example.com", "alias.apps.example.com"})

    def test_invalid_second_zone_aborts_before_any_writes(self):
        with tempfile.TemporaryDirectory() as d:
            conf = cfg(Path(d) / "state.json")
            conf["zones"] = ("apps.example.com", "other.example.com")
            responses = [{"status": "ok", "response": {"zone": {
                "name": zone, "type": kind, "disabled": False}, "records": []}}
                for zone, kind in (("apps.example.com", "Primary"), ("other.example.com", "Secondary"))]
            with patch.object(sync, "npm_hosts", return_value={"app.apps.example.com"}), \
                    patch.object(sync, "tech_call", side_effect=responses) as api:
                with self.assertRaisesRegex(RuntimeError, "enabled Primary"):
                    sync.run_once(conf)
                self.assertEqual(api.call_count, 2)
                self.assertTrue(all(call.args[1].endswith("/get") for call in api.call_args_list))
                self.assertFalse(conf["state_path"].exists())

    def test_successful_first_write_survives_later_failure_and_retry(self):
        with tempfile.TemporaryDirectory() as d:
            conf = cfg(Path(d) / "state.json")
            desired = {"a.apps.example.com", "b.apps.example.com"}
            snapshot = {}
            def write(config, endpoint, params, write=False):
                if params["domain"].startswith("b."):
                    raise sync.SyncError("API transport, TLS, or JSON failure")
                snapshot[params["domain"]] = [managed(config["ip"])]
                return {"status": "ok"}
            with patch.object(sync, "npm_hosts", return_value=desired), \
                    patch.object(sync, "dns_records", side_effect=lambda _: copy.deepcopy(snapshot)), \
                    patch.object(sync, "tech_call", side_effect=write):
                with self.assertRaises(RuntimeError):
                    sync.run_once(conf)
            self.assertEqual(sync.state_load(conf["state_path"])["owned"],
                             {"a.apps.example.com": conf["ip"]})
            with patch.object(sync, "npm_hosts", return_value=desired), \
                    patch.object(sync, "dns_records", side_effect=lambda _: copy.deepcopy(snapshot)), \
                    patch.object(sync, "tech_call", return_value={"status": "ok"}) as api:
                self.assertEqual(sync.run_once(conf)["added"], 1)
                self.assertEqual(api.call_args.args[2]["domain"], "b.apps.example.com")

    def test_orphan_with_matching_marker_is_never_automatically_adopted(self):
        with tempfile.TemporaryDirectory() as d:
            conf = cfg(Path(d) / "state.json")
            with patch.object(sync, "tech_call") as api:
                result = sync.reconcile(conf, {"app.apps.example.com"},
                                        {"app.apps.example.com": [managed(conf["ip"])]},
                                        sync.state_load(conf["state_path"]))
            self.assertEqual(result["conflicts"], 1)
            self.assertEqual(sync.state_load(conf["state_path"])["owned"], {})
            api.assert_not_called()

    def test_empty_host_domain_list_aborts_before_state_or_dns_changes(self):
        with tempfile.TemporaryDirectory() as d:
            conf = cfg(Path(d) / "state.json", deletions=True)
            state = {"version": 1, "owned": {"app.apps.example.com": conf["ip"]},
                     "stale_since": {"app.apps.example.com": 1}}
            sync.state_save(conf["state_path"], state)
            before = conf["state_path"].read_bytes()
            for enabled in (True, False):
                hosts = [{"enabled": enabled, "domain_names": []},
                         {"enabled": True, "domain_names": ["keeper.apps.example.com"]}]
                with self.subTest(enabled=enabled), \
                        patch.object(sync, "api", side_effect=[{"token": "dummy"}, hosts]), \
                        patch.object(sync, "dns_records") as read_dns, \
                        patch.object(sync, "tech_call") as write_dns:
                    with self.assertRaisesRegex(RuntimeError, "Malformed NPM domain_names"):
                        sync.run_once(conf)
                    read_dns.assert_not_called()
                    write_dns.assert_not_called()
                    self.assertEqual(conf["state_path"].read_bytes(), before)

    def test_api_rejections_remain_actionable_without_remote_secrets(self):
        class Response:
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, limit):
                return self.body[:limit]

        cases = [
            (b'{"status":"invalid-token","errorMessage":"secret-value"}', "invalid-token"),
            (b'{"status":"error","errorMessage":"secret-value"}', "rejected request"),
            (b'not-json-secret-value', "JSON failure"),
            (b'true', "invalid JSON shape"),
            (b'x' * 4_000_001, "size limit"),
        ]
        for body, hint in cases:
            with self.subTest(hint=hint), patch.object(sync.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = Response(body)
                with self.assertRaises(sync.SyncError) as raised:
                    sync.api("GET", "https://dns.example.test")
                message = sync.error_summary(raised.exception)
                self.assertIn(hint, message)
                self.assertNotIn("secret-value", message)
        for status in (401, 403, 429, 500):
            with self.subTest(status=status), patch.object(sync.urllib.request, "build_opener") as opener:
                opener.return_value.open.side_effect = urllib.error.HTTPError(
                    "https://dns.example.test", status, "secret-value", {}, None)
                with self.assertRaises(sync.SyncError) as raised:
                    sync.api("GET", "https://dns.example.test")
                self.assertEqual(sync.error_summary(raised.exception), "API HTTP status %d" % status)
        self.assertEqual(sync.error_summary(ValueError("secret-value")), "ValueError")

    def test_already_stopped_cycle_makes_no_api_calls(self):
        with tempfile.TemporaryDirectory() as d:
            conf = cfg(Path(d) / "state.json")
            conf["_stop_event"] = threading.Event()
            conf["_stop_event"].set()
            with patch.object(sync, "api") as api:
                with self.assertRaises(RuntimeError):
                    sync.run_once(conf)
                api.assert_not_called()

    def test_unwritable_state_stops_before_remote_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            conf = cfg(Path(d) / "state.json")
            with patch.object(sync, "state_save", side_effect=OSError("disk unavailable")), \
                    patch.object(sync, "dns_records", return_value={}), \
                    patch.object(sync, "tech_call", return_value={"status": "ok"}) as api:
                with self.assertRaises((OSError, RuntimeError)):
                    sync.reconcile(conf, {"app.apps.example.com"}, {}, sync.state_load(conf["state_path"]))
            api.assert_not_called()

    def test_removed_zones_do_not_consume_current_deletion_cap(self):
        with tempfile.TemporaryDirectory() as d:
            conf = cfg(Path(d) / "state.json", deletions=True)
            conf["max_deletes"] = 1
            old_names = {"old%d.other.example.com" % n: "192.0.2.20" for n in range(6)}
            owned = dict(old_names, **{"app.apps.example.com": "192.0.2.20"})
            state = {"version": 1, "owned": owned, "stale_since": {name: 1 for name in owned}}
            snapshot = {"app.apps.example.com": [managed()],
                        "keeper.apps.example.com": [{"type": "A", "rData": {"ipAddress": "192.0.2.99"}}]}
            with patch.object(sync, "dns_records", return_value=copy.deepcopy(snapshot)), \
                    patch.object(sync, "tech_call", return_value={"status": "ok"}) as api:
                result = sync.reconcile(conf, {"keeper.apps.example.com"}, snapshot, state, now=90000)
            self.assertEqual(result["deleted"], 1)
            self.assertEqual(state["owned"], old_names)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(api.call_args.args[2]["zone"], "apps.example.com")

    def test_operator_requested_stop_prevents_a_second_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            conf = cfg(Path(d) / "state.json")
            stop = threading.Event()
            conf["_stop_event"] = stop
            def write(*args, **kwargs):
                stop.set()
                return {"status": "ok"}
            with patch.object(sync, "npm_hosts", return_value={"a.apps.example.com", "b.apps.example.com"}), \
                    patch.object(sync, "dns_records", return_value={}), \
                    patch.object(sync, "tech_call", side_effect=write) as api:
                with self.assertRaises(RuntimeError):
                    sync.run_once(conf)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(sync.state_load(conf["state_path"])["owned"], {"a.apps.example.com": conf["ip"]})


if __name__ == "__main__":
    unittest.main()
