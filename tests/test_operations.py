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
                with self.assertRaisesRegex(RuntimeError, "enabled Primary or Forwarder"):
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
                    patch.object(sync, "dns_records", side_effect=lambda *_, **__: copy.deepcopy(snapshot)), \
                    patch.object(sync, "tech_call", side_effect=write):
                with self.assertRaises(RuntimeError):
                    sync.run_once(conf)
            self.assertEqual(sync.state_load(conf["state_path"])["owned"],
                             {"a.apps.example.com": conf["ip"]})
            with patch.object(sync, "npm_hosts", return_value=desired), \
                    patch.object(sync, "dns_records", side_effect=lambda *_, **__: copy.deepcopy(snapshot)), \
                    patch.object(sync, "tech_call", return_value={"status": "ok"}) as api:
                self.assertEqual(sync.run_once(conf)["added"], 1)
                self.assertEqual(api.call_args.args[2]["domain"], "b.apps.example.com")

    def test_mixed_primary_and_nested_forwarder_zones_route_by_longest_match(self):
        with tempfile.TemporaryDirectory() as d:
            conf = cfg(Path(d) / "state.json")
            conf["zones"] = ("example.com", "lan.example.com")
            types = {"example.com": "Primary", "lan.example.com": "Forwarder"}
            writes = []
            def fake(config, path, params=None, write=False):
                if path.endswith("/get"):
                    zone = params["zone"]
                    stored = [{"name": d, "type": "A", "disabled": False, "comments": sync.MARKER, "ttl": 120,
                               "rData": {"ipAddress": config["ip"]}} for z, d in writes if z == zone]
                    return {"status": "ok", "response": {"zone": {"name": zone, "type": types[zone], "disabled": False},
                            "records": [{"name": zone, "type": "SOA", "disabled": False, "rData": {}}] + stored}}
                writes.append((params["zone"], params["domain"]))
                return {"status": "ok"}
            with patch.object(sync, "npm_hosts", return_value={"app.lan.example.com", "www.example.com"}),                     patch.object(sync, "tech_call", side_effect=fake) as api:
                self.assertEqual(sync.run_once(conf)["added"], 2)
            # Only the Forwarder zone gets the HTTPS companion (A + HTTPS writes).
            self.assertEqual(sorted(writes), [("example.com", "www.example.com"),
                                              ("lan.example.com", "app.lan.example.com"),
                                              ("lan.example.com", "app.lan.example.com")])
            rereads = [c.args[2]["zone"] for c in api.call_args_list if c.args[1].endswith("/get")][2:]
            self.assertEqual(rereads, ["lan.example.com", "lan.example.com", "example.com"])

    def test_nested_zone_apex_is_not_desired_but_its_children_are(self):
        conf = cfg(Path("unused"))
        conf["zones"] = ("example.com", "internal.example.com")
        hosts = [{"enabled": True, "domain_names": [
            "portal.example.com", "internal.example.com", "db.internal.example.com", "a.b.internal.example.com"]}]
        with patch.object(sync, "api", side_effect=[{"token": "t"}, hosts]):
            desired = sync.npm_hosts(conf)
        self.assertEqual(desired, {"portal.example.com", "db.internal.example.com", "a.b.internal.example.com"})
        self.assertEqual({sync.zone_for(n, conf["zones"]) for n in desired - {"portal.example.com"}},
                         {"internal.example.com"})

    def test_nested_zone_shadows_parent_records_in_snapshot(self):
        conf = cfg(Path("unused"))
        conf["zones"] = ("example.com", "internal.example.com")
        listings = {
            "example.com": [{"name": "example.com", "type": "SOA", "disabled": False, "rData": {}},
                            {"name": "internal.example.com", "type": "A", "disabled": False,
                             "comments": sync.MARKER, "rData": {"ipAddress": "192.0.2.20"}},
                            {"name": "old.internal.example.com", "type": "A", "disabled": False, "rData": {}}],
            "internal.example.com": [{"name": "internal.example.com", "type": "SOA", "disabled": False, "rData": {}},
                                     {"name": "db.internal.example.com", "type": "A", "disabled": False, "rData": {}}]}
        def fake(config, path, params=None, write=False):
            zone = params["zone"]
            return {"status": "ok", "response": {"zone": {"name": zone, "type": "Primary", "disabled": False},
                                                 "records": listings[zone]}}
        with patch.object(sync, "tech_call", side_effect=fake):
            snapshot = sync.dns_records(conf)
        self.assertEqual([r["type"] for r in snapshot["internal.example.com"]], ["SOA"])
        self.assertNotIn("old.internal.example.com", snapshot)
        self.assertIn("db.internal.example.com", snapshot)

    def test_owned_name_that_became_a_listed_zone_apex_is_released(self):
        with tempfile.TemporaryDirectory() as d:
            conf = cfg(Path(d) / "state.json")
            conf["zones"] = ("apps.example.com", "internal.apps.example.com")
            state = {"version": 1, "owned": {"internal.apps.example.com": conf["ip"]}, "stale_since": {}}
            with patch.object(sync, "tech_call") as api, self.assertLogs(sync.LOG, "WARNING") as logs:
                result = sync.reconcile(conf, set(), {}, state)
            self.assertEqual(result["conflicts"], 1)
            api.assert_not_called()
            self.assertEqual(sync.state_load(conf["state_path"])["owned"], {})
            self.assertTrue(any("Stopped managing internal.apps.example.com" in line for line in logs.output))

    def test_pre_write_reread_reads_only_the_target_zone(self):
        conf = cfg(Path("/nonexistent/state.json"))
        conf["zones"] = ("apps.example.com", "lab.example.net")
        with patch.object(sync, "dns_records", return_value={}) as read_dns:
            sync.check_unchanged(conf, "app.lab.example.net", [])
        read_dns.assert_called_once_with(conf, ("lab.example.net",))

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
