"""Local HTTP fixtures exercise the wire format; no live management endpoints."""
import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs

import sync
from test_sync import cfg


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conf = cfg(Path(self.temp.name) / "state.json")
        self.records = []
        self.hosts = [{"enabled": True, "domain_names": ["app.apps.example.com"]}]
        self.writes = []
        self.requests = []
        self.queried = False
        self.zone_type = "Primary"
        self.reject = {}  # domain -> Technitium response for that write
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, value):
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fixture.requests.append((self.path, self.headers.get("Authorization")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/sink")
                    self.end_headers()
                elif self.path == "/api/nginx/proxy-hosts":
                    if self.headers.get("Authorization") != "Bearer npm-session":
                        return self.respond({"error": "unauthorized"})
                    self.respond(fixture.hosts)
                else:
                    self.respond({})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/api/tokens":
                    auth = json.loads(body)
                    fixture.assertEqual(auth, {"identity": fixture.conf["npm_identity"], "secret": "x"})
                    return self.respond({"token": "npm-session"})
                fixture.assertEqual(self.headers.get("Content-Type"), "application/x-www-form-urlencoded")
                params = {k: v[0] for k, v in parse_qs(body.decode()).items()}
                if fixture.conf.get("tech_auth") == "parameter":
                    fixture.assertEqual(params.pop("token"), "x")
                    fixture.assertIsNone(self.headers.get("Authorization"))
                else:
                    fixture.assertEqual(self.headers.get("Authorization"), "Bearer x")
                    fixture.assertNotIn("token", params)
                if self.path.endswith("/get"):
                    if fixture.queried:
                        # Real Technitium 15.6.2 rewrites lastUsedOn whenever a record answers.
                        for record in fixture.records:
                            record["lastUsedOn"] = "2026-11-11T08:23:%09.6fZ" % (time.time() % 60)
                    return self.respond({"status": "ok", "response": {
                        "zone": {"name": "apps.example.com", "disabled": False, "type": fixture.zone_type()
                                 if callable(fixture.zone_type) else fixture.zone_type},
                        "records": fixture.records}})
                fixture.writes.append((self.path, params))
                if params.get("domain") in fixture.reject:
                    return self.respond(fixture.reject[params["domain"]])
                if self.path.endswith("/add") and params["type"] == "HTTPS":
                    # Shape returned by real Technitium 15.6.2 for svcTargetName ".".
                    key, value = params["svcParams"].split("|")
                    fixture.records.append({"name": params["domain"], "type": "HTTPS", "disabled": False,
                                            "comments": params["comments"], "ttl": int(params["ttl"]),
                                            "rData": {"svcPriority": int(params["svcPriority"]),
                                                      "svcTargetName": "", "svcParams": {key: value},
                                                      "autoIpv4Hint": False, "autoIpv6Hint": False}})
                elif self.path.endswith("/add"):
                    fixture.records.append({"name": params["domain"], "type": "A", "disabled": False,
                                            "comments": params["comments"], "ttl": int(params["ttl"]),
                                            "rData": {"ipAddress": params["ipAddress"]}})
                elif self.path.endswith("/update"):
                    record = next(r for r in fixture.records if r["name"] == params["domain"] and r["type"] == "A")
                    fixture.assertEqual(record["rData"]["ipAddress"], params["ipAddress"])
                    record["rData"]["ipAddress"] = params["newIpAddress"]
                    record["ttl"] = int(params["ttl"])
                elif self.path.endswith("/delete"):
                    fixture.records[:] = [r for r in fixture.records
                                          if not (r["name"] == params["domain"] and r["type"] == params["type"])]
                else:
                    fixture.fail("Unexpected endpoint")
                self.respond({"status": "ok"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def close():
            self.server.shutdown()
            self.server.server_close()
            thread.join(2)
        self.addCleanup(close)
        self.conf["npm_url"] = self.conf["dns_url"] = "http://127.0.0.1:%d" % self.server.server_port

    def test_full_lifecycle_and_restart(self):
        self.conf["dry_run"] = True
        self.assertEqual(sync.run_once(self.conf)["added"], 1)
        self.assertFalse(self.conf["state_path"].exists())
        self.assertTrue((self.conf["state_path"].parent / "last_success").exists())
        self.assertEqual(self.writes, [])
        self.conf["dry_run"] = False
        self.assertEqual(sync.run_once(self.conf)["added"], 1)
        self.assertEqual(sync.run_once(self.conf)["added"], 0)
        self.conf["ip"] = "192.0.2.21"
        self.assertEqual(sync.run_once(self.conf)["updated"], 1)
        self.assertEqual(self.records[0]["rData"]["ipAddress"], "192.0.2.21")
        self.conf.update(deletions=True, allow_empty=True)
        self.hosts = []
        self.assertEqual(sync.run_once(self.conf)["pending"], 1)
        state = sync.state_load(self.conf["state_path"])
        state["stale_since"]["app.apps.example.com"] = time.time() - 86401
        sync.state_save(self.conf["state_path"], state)
        self.assertEqual(sync.run_once(self.conf)["deleted"], 1)
        self.assertEqual(self.records, [])
        self.assertEqual(sync.state_load(self.conf["state_path"])["owned"], {})

    def test_legacy_auth_body(self):
        self.conf["tech_auth"] = "parameter"
        self.assertEqual(sync.run_once(self.conf)["added"], 1)

    def test_owned_ttl_change_is_reconciled(self):
        sync.run_once(self.conf)
        self.conf["ttl"] = 300
        self.assertEqual(sync.run_once(self.conf)["updated"], 1)
        self.assertEqual(self.records[0]["ttl"], 300)
        self.assertEqual(sync.run_once(self.conf)["updated"], 0)

    def test_service_dns_owners_are_preserved(self):
        self.records = [{"name": "_acme-challenge.apps.example.com", "type": "TXT",
                         "disabled": False, "rData": {"text": "dummy-challenge"}},
                        {"name": "_sip._tcp.apps.example.com", "type": "SRV",
                         "disabled": False, "rData": {"target": "sip.example.com"}}]
        manual = copy.deepcopy(self.records)
        self.assertEqual(sync.run_once(self.conf)["added"], 1)
        self.assertEqual(self.records[:2], manual)

    def test_blocked_return_resets_delete_grace_timer(self):
        for blocker in ("NS", "FWD", "DNAME", "CNAME"):
            with self.subTest(blocker=blocker):
                self.conf["state_path"].unlink(missing_ok=True)
                self.records = []
                self.writes.clear()
                self.conf.update(deletions=True, delete_grace=300, allow_empty=False)
                self.hosts = [{"enabled": True, "domain_names": ["app.branch.apps.example.com", "other.apps.example.com"]}]
                with patch.object(sync.time, "time", return_value=1):
                    sync.run_once(self.conf)
                self.hosts[0]["domain_names"] = ["other.apps.example.com"]
                with patch.object(sync.time, "time", return_value=1):
                    self.assertEqual(sync.run_once(self.conf)["pending"], 1)
                self.assertEqual(sync.state_load(self.conf["state_path"])["stale_since"]["app.branch.apps.example.com"], 1)

                self.hosts[0]["domain_names"] = ["app.branch.apps.example.com", "other.apps.example.com"]
                self.records.append({"name": "branch.apps.example.com", "type": blocker, "disabled": False,
                                     "rData": {"nameServer": "ns.example.net"}})
                with patch.object(sync.time, "time", return_value=100000):
                    result = sync.run_once(self.conf)
                self.assertEqual(result["conflicts"], 1)
                self.assertNotIn("app.branch.apps.example.com", sync.state_load(self.conf["state_path"])["stale_since"])

                self.hosts[0]["domain_names"] = ["other.apps.example.com"]
                self.records = [r for r in self.records if r["name"] != "branch.apps.example.com"]
                with patch.object(sync.time, "time", return_value=100001):
                    result = sync.run_once(self.conf)
                self.assertEqual(result["pending"], 1)
                self.assertEqual(sync.state_load(self.conf["state_path"])["stale_since"]["app.branch.apps.example.com"], 100001)
                self.assertEqual([w for w in self.writes if w[0].endswith("/delete")], [])

                with patch.object(sync.time, "time", return_value=100001 + 299):
                    self.assertEqual(sync.run_once(self.conf)["deleted"], 0)
                with patch.object(sync.time, "time", return_value=100001 + 300):
                    self.assertEqual(sync.run_once(self.conf)["deleted"], 1)
                deletes = [w for w in self.writes if w[0].endswith("/delete")]
                self.assertEqual([w[1]["domain"] for w in deletes], ["app.branch.apps.example.com"])

    def test_apex_dname_and_cname_guard_children(self):
        for record_type in ("DNAME", "CNAME"):
            with self.subTest(record_type=record_type):
                self.records = [{"name": "apps.example.com", "type": record_type,
                                 "disabled": False, "rData": {"target": "alias.example.net"}}]
                self.writes.clear()
                self.conf["dry_run"] = True
                self.assertEqual(sync.run_once(self.conf)["conflicts"], 1)
                self.assertEqual(self.writes, [])
                self.conf["dry_run"] = False
                self.assertEqual(sync.run_once(self.conf)["conflicts"], 1)
                self.assertEqual(self.writes, [])

    def test_apex_alias_added_on_final_reread_refuses_write(self):
        for record_type in ("DNAME", "CNAME"):
            with self.subTest(record_type=record_type):
                self.conf["state_path"].unlink(missing_ok=True)
                self.records = []
                self.writes.clear()
                before = {}
                after = {"apps.example.com": [{"name": "apps.example.com", "type": record_type,
                                                "disabled": False, "rData": {"target": "elsewhere.example.net"}}]}
                with patch.object(sync, "dns_records", side_effect=[before, after]):
                    with self.assertRaises(RuntimeError):
                        sync.run_once(self.conf)
                self.assertEqual(self.writes, [])

    # Apex shape returned by real Technitium 15.6.2 for a Forwarder zone.
    FORWARDER_APEX = [
        {"name": "apps.example.com", "type": "SOA", "ttl": 0, "disabled": False, "rData": {"serial": 1}},
        {"name": "apps.example.com", "type": "FWD", "ttl": 0, "disabled": False,
         "rData": {"protocol": "Udp", "forwarder": "192.0.2.53", "priority": 0}}]

    def test_forwarder_zone_full_lifecycle(self):
        self.zone_type = "Forwarder"
        self.records = copy.deepcopy(self.FORWARDER_APEX)
        self.assertEqual(sync.run_once(self.conf)["added"], 1)
        self.assertEqual(sync.run_once(self.conf)["added"], 0)
        self.conf["ip"] = "192.0.2.21"
        self.assertEqual(sync.run_once(self.conf)["updated"], 1)
        self.conf.update(deletions=True, allow_empty=True)
        self.hosts = []
        sync.run_once(self.conf)
        state = sync.state_load(self.conf["state_path"])
        state["stale_since"]["app.apps.example.com"] = time.time() - 86401
        sync.state_save(self.conf["state_path"], state)
        self.assertEqual(sync.run_once(self.conf)["deleted"], 1)
        self.assertEqual(self.records, self.FORWARDER_APEX)  # Forwarding rule untouched.

    def test_replica_and_unknown_zone_types_refuse_all_writes(self):
        for kind in ("Secondary", "SecondaryForwarder", "Stub", "Catalog", "SecondaryCatalog", "primary", None):
            with self.subTest(kind=kind):
                self.zone_type = kind
                with self.assertRaisesRegex(RuntimeError, "enabled Primary or Forwarder"):
                    sync.run_once(self.conf)
                self.assertEqual(self.writes, [])

    def test_subtree_forwarding_blocks_names_beneath_it(self):
        for zone_type in ("Forwarder", "Primary"):
            with self.subTest(zone_type=zone_type):
                self.conf["state_path"].unlink(missing_ok=True)
                self.zone_type = zone_type
                self.writes.clear()
                self.records = copy.deepcopy(self.FORWARDER_APEX) + [
                    {"name": "branch.apps.example.com", "type": "FWD", "disabled": False,
                     "rData": {"protocol": "Udp", "forwarder": "192.0.2.54"}}]
                self.hosts = [{"enabled": True, "domain_names": ["app.branch.apps.example.com", "app.apps.example.com"]}]
                result = sync.run_once(self.conf)
                self.assertEqual((result["added"], result["conflicts"]), (1, 1))
                # Forwarder zones also get the HTTPS companion for the written name.
                expected = [("app.apps.example.com", "A")] + (
                    [("app.apps.example.com", "HTTPS")] if zone_type == "Forwarder" else [])
                self.assertEqual([(w[1]["domain"], w[1]["type"]) for w in self.writes], expected)

    def test_subtree_forwarding_added_on_final_reread_refuses_write(self):
        self.zone_type = "Forwarder"
        self.hosts = [{"enabled": True, "domain_names": ["app.branch.apps.example.com"]}]
        apex = {"apps.example.com": copy.deepcopy(self.FORWARDER_APEX)}
        after = dict(apex, **{"branch.apps.example.com": [
            {"name": "branch.apps.example.com", "type": "FWD", "disabled": False, "rData": {}}]})
        with patch.object(sync, "dns_records", side_effect=[apex, after]):
            with self.assertRaisesRegex(RuntimeError, "forwarding"):
                sync.run_once(self.conf)
        self.assertEqual(self.writes, [])

    def test_npm_name_that_owns_forwarding_is_unmanaged(self):
        self.zone_type = "Forwarder"
        self.records = copy.deepcopy(self.FORWARDER_APEX) + [
            {"name": "branch.apps.example.com", "type": "FWD", "disabled": False, "rData": {"forwarder": "192.0.2.54"}}]
        self.hosts = [{"enabled": True, "domain_names": ["branch.apps.example.com"]}]
        result = sync.run_once(self.conf)
        self.assertEqual((result["added"], result["conflicts"]), (0, 1))
        self.assertEqual(self.writes, [])

    def test_forwarding_added_above_owned_name_skips_delete_and_cycle_completes(self):
        self.zone_type = "Forwarder"
        self.records = copy.deepcopy(self.FORWARDER_APEX)
        self.conf.update(deletions=True, delete_grace=300)
        self.hosts = [{"enabled": True, "domain_names": ["app.branch.apps.example.com", "keep.apps.example.com"]}]
        self.assertEqual(sync.run_once(self.conf)["added"], 2)
        self.hosts = [{"enabled": True, "domain_names": ["keep.apps.example.com"]}]
        self.records.append({"name": "branch.apps.example.com", "type": "FWD", "disabled": False,
                             "rData": {"forwarder": "192.0.2.54"}})
        state = sync.state_load(self.conf["state_path"])
        state["stale_since"]["app.branch.apps.example.com"] = 1
        sync.state_save(self.conf["state_path"], state)
        heartbeat = self.conf["state_path"].parent / "last_success"
        heartbeat.unlink()
        self.writes.clear()
        result = sync.run_once(self.conf)  # Must not raise.
        self.assertEqual((result["deleted"], result["conflicts"]), (0, 1))
        self.assertEqual(self.writes, [])
        self.assertTrue(heartbeat.exists())
        self.assertIn("app.branch.apps.example.com", sync.state_load(self.conf["state_path"])["owned"])

    def test_zone_type_change_before_write_refuses(self):
        types = iter(["Forwarder", "SecondaryForwarder"])
        self.zone_type = lambda: next(types)
        self.records = copy.deepcopy(self.FORWARDER_APEX)
        with self.assertRaisesRegex(RuntimeError, "enabled Primary or Forwarder"):
            sync.run_once(self.conf)
        self.assertEqual(self.writes, [])

    CHILD_ZONE_ERROR = {"status": "error", "errorMessage":
                        "The domain name 'database.internal.apps.example.com' does not belong to the zone: apps.example.com"}

    def test_name_in_separate_child_zone_is_skipped_and_others_continue(self):
        self.reject = {"database.internal.apps.example.com": self.CHILD_ZONE_ERROR}
        self.hosts = [{"enabled": True, "domain_names": [
            "api.apps.example.com", "database.internal.apps.example.com", "wiki.apps.example.com"]}]
        with self.assertLogs(sync.LOG, "WARNING") as logs:
            result = sync.run_once(self.conf)
        self.assertEqual((result["added"], result["conflicts"]), (2, 1))
        self.assertEqual(sorted(sync.state_load(self.conf["state_path"])["owned"]),
                         ["api.apps.example.com", "wiki.apps.example.com"])
        self.assertTrue(any("separate zone" in line for line in logs.output))
        self.assertTrue((self.conf["state_path"].parent / "last_success").exists())
        self.assertEqual(sync.run_once(self.conf)["added"], 0)  # Retried each cycle, never owned.

    def test_rejected_update_and_delete_keep_ownership_and_fail_cycle_after_all_names(self):
        self.hosts = [{"enabled": True, "domain_names": ["app.apps.example.com", "zzz.apps.example.com"]}]
        sync.run_once(self.conf)
        self.reject = {"app.apps.example.com": {"status": "error", "errorMessage": "Access was denied."}}
        self.conf["ip"] = "192.0.2.21"
        heartbeat = self.conf["state_path"].parent / "last_success"
        heartbeat.unlink()
        with self.assertRaisesRegex(RuntimeError, "rejected 1 write"):
            sync.run_once(self.conf)
        self.assertFalse(heartbeat.exists())  # Healthcheck reports the problem.
        self.assertEqual(sync.state_load(self.conf["state_path"])["owned"],
                         {"app.apps.example.com": "192.0.2.20", "zzz.apps.example.com": "192.0.2.21"})
        self.conf.update(ip="192.0.2.20", deletions=True, allow_empty=True)
        self.hosts = []
        sync.run_once(self.conf)
        state = sync.state_load(self.conf["state_path"])
        state["stale_since"]["app.apps.example.com"] = time.time() - 86401
        sync.state_save(self.conf["state_path"], state)
        with self.assertLogs(sync.LOG, "WARNING") as logs, self.assertRaisesRegex(RuntimeError, "rejected 1 write"):
            sync.run_once(self.conf)
        self.assertIn("app.apps.example.com", sync.state_load(self.conf["state_path"])["owned"])
        self.assertTrue(any("permissions" in line for line in logs.output))
        self.assertFalse(any("Access was denied" in line for line in logs.output))  # Remote text never logged.

    def test_invalid_token_on_write_still_aborts_cycle(self):
        self.reject = {"app.apps.example.com": {"status": "invalid-token"}}
        with self.assertRaisesRegex(RuntimeError, "invalid-token"):
            sync.run_once(self.conf)

    def by_type(self, name):
        return {r["type"]: r for r in self.records if r["name"] == name}

    def test_forwarder_zone_adds_https_companion_without_http3(self):
        self.zone_type = "Forwarder"
        self.records = copy.deepcopy(self.FORWARDER_APEX)
        self.assertEqual(sync.run_once(self.conf)["added"], 1)
        https = [w[1] for w in self.writes if w[1]["type"] == "HTTPS"]
        self.assertEqual(len(https), 1)
        self.assertEqual({k: https[0][k] for k in ("svcPriority", "svcTargetName", "svcParams",
                                                   "autoIpv4Hint", "autoIpv6Hint", "comments", "overwrite")},
                         {"svcPriority": "1", "svcTargetName": ".", "svcParams": "alpn|h2,http/1.1",
                          "autoIpv4Hint": "false", "autoIpv6Hint": "false", "comments": sync.MARKER,
                          "overwrite": "false"})
        self.assertEqual(set(self.by_type("app.apps.example.com")), {"A", "HTTPS"})
        self.writes.clear()
        self.assertEqual(sync.run_once(self.conf), {"added": 0, "updated": 0, "deleted": 0, "conflicts": 0, "pending": 0})
        self.assertEqual(self.writes, [])

    def test_primary_zone_never_gets_https_companion(self):
        self.assertEqual(sync.run_once(self.conf)["added"], 1)
        self.assertEqual([w[1]["type"] for w in self.writes], ["A"])

    def test_existing_owned_record_gets_companion_once(self):
        sync.run_once(self.conf)  # Created as plain A (as 1.0.5 did, or in a Primary zone).
        self.zone_type = "Forwarder"
        self.writes.clear()
        self.assertEqual(sync.run_once(self.conf)["updated"], 1)
        self.assertEqual([w[1]["type"] for w in self.writes], ["HTTPS"])
        self.writes.clear()
        self.assertEqual(sync.run_once(self.conf)["updated"], 0)
        self.assertEqual(self.writes, [])

    def test_dry_run_plans_companion_without_writing(self):
        sync.run_once(self.conf)
        self.zone_type, self.conf["dry_run"] = "Forwarder", True
        self.writes.clear()
        with self.assertLogs(sync.LOG, "INFO") as logs:
            self.assertEqual(sync.run_once(self.conf)["updated"], 1)
        self.assertEqual(self.writes, [])
        self.assertTrue(any("PLAN ADD app.apps.example.com HTTPS" in line for line in logs.output))

    def test_manual_or_different_https_record_is_a_conflict(self):
        self.zone_type = "Forwarder"
        sync.run_once(self.conf)
        for change in ({"svcParams": {"alpn": "h3,h2"}}, {"comments": "manual"}):
            with self.subTest(change=change):
                record = self.by_type("app.apps.example.com")["HTTPS"]
                original = copy.deepcopy(record)
                record.update(change) if "comments" in change else record["rData"].update(change)
                self.writes.clear()
                self.conf["ip"] = "192.0.2.21"
                self.assertEqual(sync.run_once(self.conf)["conflicts"], 1)
                self.assertEqual(self.writes, [])
                record.clear(); record.update(original)
                self.conf["ip"] = "192.0.2.20"

    def test_ip_update_keeps_companion_and_deletion_removes_both(self):
        self.zone_type = "Forwarder"
        self.records = copy.deepcopy(self.FORWARDER_APEX)
        sync.run_once(self.conf)
        companion = copy.deepcopy(self.by_type("app.apps.example.com")["HTTPS"])
        self.conf["ip"] = "192.0.2.21"
        self.writes.clear()
        self.assertEqual(sync.run_once(self.conf)["updated"], 1)
        self.assertEqual([w[1]["type"] for w in self.writes], ["A"])
        self.assertEqual(self.by_type("app.apps.example.com")["HTTPS"], companion)
        self.conf.update(deletions=True, allow_empty=True)
        self.hosts = []
        sync.run_once(self.conf)
        state = sync.state_load(self.conf["state_path"])
        state["stale_since"]["app.apps.example.com"] = time.time() - 86401
        sync.state_save(self.conf["state_path"], state)
        self.writes.clear()
        self.assertEqual(sync.run_once(self.conf)["deleted"], 1)
        self.assertEqual([(w[1]["type"], w[1].get("svcParams")) for w in self.writes],
                         [("HTTPS", "alpn|h2,http/1.1"), ("A", None)])
        self.assertEqual(self.records, self.FORWARDER_APEX)

    def test_marked_https_without_state_is_never_adopted(self):
        self.zone_type = "Forwarder"
        sync.run_once(self.conf)
        self.conf["state_path"].unlink()
        self.writes.clear()
        result = sync.run_once(self.conf)
        self.assertEqual((result["added"], result["conflicts"]), (0, 1))
        self.assertEqual(self.writes, [])

    def test_rejected_companion_keeps_a_record_owned_and_retries(self):
        self.zone_type = "Forwarder"
        sync.run_once(self.conf)
        self.records = [r for r in self.records if r["type"] != "HTTPS"]
        self.reject = {"app.apps.example.com": {"status": "error", "errorMessage": "Access was denied."}}
        with self.assertRaisesRegex(RuntimeError, "rejected 1 write"):
            sync.run_once(self.conf)
        self.assertIn("app.apps.example.com", sync.state_load(self.conf["state_path"])["owned"])
        self.reject = {}
        self.assertEqual(sync.run_once(self.conf)["updated"], 1)
        self.assertIn("HTTPS", self.by_type("app.apps.example.com"))

    def test_forget_missing_recreates_records_deleted_by_hand(self):
        self.hosts = [{"enabled": True, "domain_names": ["a.apps.example.com", "b.apps.example.com", "c.apps.example.com"]}]
        self.assertEqual(sync.run_once(self.conf)["added"], 3)
        # Operator deletes a and b by mistake; c is edited by hand (kept, not "missing").
        self.records = [r for r in self.records if r["name"] not in ("a.apps.example.com", "b.apps.example.com")]
        self.records[0]["rData"]["ipAddress"] = "192.0.2.99"
        result = sync.run_once(self.conf)
        self.assertEqual((result["added"], result["conflicts"]), (0, 3))
        self.writes.clear()
        with self.assertLogs(sync.LOG, "WARNING"):
            self.assertEqual(sync.admin(self.conf, ["--forget-missing"]), 0)
        self.assertEqual(self.writes, [])  # Releasing never writes DNS.
        self.assertEqual(sorted(sync.state_load(self.conf["state_path"])["owned"]), ["c.apps.example.com"])
        result = sync.run_once(self.conf)
        self.assertEqual((result["added"], result["conflicts"]), (2, 1))
        self.assertEqual(sorted(r["name"] for r in self.records if r["type"] == "A"),
                         ["a.apps.example.com", "b.apps.example.com", "c.apps.example.com"])

    def test_forget_patterns_and_all_release_ownership_only(self):
        self.hosts = [{"enabled": True, "domain_names": [
            "portal.apps.example.com", "db.internal.apps.example.com", "x.y.internal.apps.example.com"]}]
        sync.run_once(self.conf)
        before = copy.deepcopy(self.records)
        sync.admin(self.conf, ["--forget", "*.INTERNAL.apps.example.com."])
        self.assertEqual(sorted(sync.state_load(self.conf["state_path"])["owned"]), ["portal.apps.example.com"])
        sync.admin(self.conf, ["--forget", "nothing.example.org"])
        self.assertEqual(len(sync.state_load(self.conf["state_path"])["owned"]), 1)
        sync.admin(self.conf, ["--forget-all"])
        self.assertEqual(sync.state_load(self.conf["state_path"])["owned"], {})
        self.assertEqual(self.records, before)
        # Existing records are now unmanaged and are never adopted.
        self.assertEqual(sync.run_once(self.conf)["conflicts"], 3)
        for bad in (["--forget"], ["--forget", " "], ["--forget-bogus"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sync.admin(self.conf, bad)

    def test_list_owned_prints_state(self):
        sync.run_once(self.conf)
        with patch("builtins.print") as out:
            self.assertEqual(sync.admin(self.conf, ["--list-owned"]), 0)
        self.assertEqual(out.call_args.args[0], "app.apps.example.com A 192.0.2.20")

    def test_cli_reports_usage_missing_setting_and_busy_lock(self):
        def run(args, env):
            return subprocess.run([sys.executable, str(Path(sync.__file__))] + args, env=env,
                                  capture_output=True, text=True, timeout=10)
        base = {"PATH": os.environ.get("PATH", "")}
        self.assertIn("--forget-missing", run(["--bogus"], base).stderr)
        self.assertIn("Missing required setting: NPM_URL", run(["--check"], base).stderr)
        env = dict(base, NPM_URL="https://npm.example.test", NPM_IDENTITY="r@example.test", NPM_SECRET="dummy-value",
                   TECHNITIUM_URL="https://dns.example.test", TECHNITIUM_TOKEN="dummy-token", NPM_LAN_IP="192.0.2.20",
                   DNS_ZONES="apps.example.com", STATE_FILE=str(self.conf["state_path"]))
        lock_path = self.conf["state_path"].parent / "sync.lock"
        with lock_path.open("a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run(["--list-owned"], env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("docker compose stop", result.stderr)
        self.assertNotIn("dummy-value", result.stderr + result.stdout)

    def test_owned_name_moved_into_listed_nested_zone_is_recreated_there(self):
        self.hosts = [{"enabled": True, "domain_names": ["db.internal.apps.example.com"]}]
        sync.run_once(self.conf)  # Created in the parent before the nested zone existed.
        self.conf["zones"] = ("apps.example.com", "internal.apps.example.com")
        shadow = {"db.internal.apps.example.com": copy.deepcopy(self.records)}
        state = sync.state_load(self.conf["state_path"])
        written = []
        def fake(config, path, params=None, write=False):
            written.append((params["zone"], params["domain"]))
            return {"status": "ok"}
        with patch.object(sync, "tech_call", side_effect=fake), patch.object(sync, "check_unchanged"),                 self.assertLogs(sync.LOG, "WARNING") as logs:
            result = sync.reconcile(self.conf, {"db.internal.apps.example.com"}, {}, state, shadowed=shadow)
        self.assertEqual((result["added"], result["conflicts"]), (1, 0))
        self.assertEqual(written, [("internal.apps.example.com", "db.internal.apps.example.com")])
        self.assertTrue(any("Moved db.internal.apps.example.com" in line for line in logs.output))
        # A shadowed record that is not this tool's (no marker) is never treated as ours.
        state = {"version": 1, "owned": {"db.internal.apps.example.com": "192.0.2.20"}, "stale_since": {}}
        shadow["db.internal.apps.example.com"][0]["comments"] = "manual"
        with patch.object(sync, "tech_call", side_effect=fake):
            result = sync.reconcile(self.conf, {"db.internal.apps.example.com"}, {}, state, shadowed=shadow)
        self.assertEqual((result["added"], result["conflicts"]), (0, 1))

    def test_update_plus_companion_counts_once(self):
        sync.run_once(self.conf)
        self.zone_type = "Forwarder"
        self.conf["ip"] = "192.0.2.21"
        self.assertEqual(sync.run_once(self.conf)["updated"], 1)
        self.assertEqual(set(self.by_type("app.apps.example.com")), {"A", "HTTPS"})

    def test_https_companion_can_be_disabled_and_reenabled(self):
        self.zone_type = "Forwarder"
        self.records = copy.deepcopy(self.FORWARDER_APEX)
        self.conf["https_records"] = False
        self.assertEqual(sync.run_once(self.conf)["added"], 1)
        self.assertEqual([w[1]["type"] for w in self.writes], ["A"])
        self.conf["https_records"] = True
        self.writes.clear()
        self.assertEqual(sync.run_once(self.conf)["updated"], 1)
        self.assertEqual([w[1]["type"] for w in self.writes], ["HTTPS"])
        # Disabling removes the tool's own companion, and only that.
        self.conf["https_records"] = False
        self.writes.clear()
        self.conf["dry_run"] = True
        with self.assertLogs(sync.LOG, "INFO") as logs:
            self.assertEqual(sync.run_once(self.conf)["updated"], 1)
        self.assertEqual(self.writes, [])
        self.assertTrue(any("PLAN DELETE app.apps.example.com HTTPS" in line for line in logs.output))
        self.conf["dry_run"] = False
        self.assertEqual(sync.run_once(self.conf)["updated"], 1)
        self.assertEqual([(w[0].rsplit("/", 1)[1], w[1]["type"], w[1]["svcParams"]) for w in self.writes],
                         [("delete", "HTTPS", "alpn|h2,http/1.1")])
        self.assertEqual(set(self.by_type("app.apps.example.com")), {"A"})
        self.writes.clear()
        self.assertEqual(sync.run_once(self.conf)["updated"], 0)
        self.assertEqual(self.writes, [])

    def test_disabling_https_never_removes_a_manual_https_record(self):
        self.zone_type = "Forwarder"
        sync.run_once(self.conf)
        self.by_type("app.apps.example.com")["HTTPS"]["comments"] = "manual"
        self.conf["https_records"] = False
        self.writes.clear()
        self.assertEqual(sync.run_once(self.conf)["conflicts"], 1)
        self.assertEqual(self.writes, [])

    def test_normal_apex_soa_and_ns_allow_child_add(self):
        self.records = [{"name": "apps.example.com", "type": "SOA", "disabled": False, "rData": {}},
                        {"name": "apps.example.com", "type": "NS", "disabled": False, "rData": {}}]
        apex = copy.deepcopy(self.records)
        self.assertEqual(sync.run_once(self.conf)["added"], 1)
        self.assertEqual(self.records[:2], apex)
        self.assertEqual(len([r for r in self.records if r["name"] == "app.apps.example.com"]), 1)

    def test_returning_name_timer_reset_persists_before_later_write_failure(self):
        self.conf.update(deletions=True, delete_grace=300, allow_empty=False)
        sync.run_once(self.conf)
        state = sync.state_load(self.conf["state_path"])
        state["stale_since"]["app.apps.example.com"] = 1
        sync.state_save(self.conf["state_path"], state)
        self.hosts = [{"enabled": True,
                       "domain_names": ["aaa.apps.example.com", "app.apps.example.com"]}]
        original_tech_call = sync.tech_call

        def fail_new_add(cfg, path, params, write=False):
            if path.endswith("/add"):
                raise RuntimeError("simulated add failure")
            return original_tech_call(cfg, path, params, write=write)

        with patch.object(sync, "tech_call", side_effect=fail_new_add):
            with self.assertRaisesRegex(RuntimeError, "simulated add failure"):
                sync.run_once(self.conf)
        self.assertNotIn("app.apps.example.com",
                         sync.state_load(self.conf["state_path"])["stale_since"])

    def test_malformed_source_never_writes_or_deletes(self):
        sync.run_once(self.conf)
        self.writes.clear()
        self.hosts = [{"enabled": "false", "domain_names": ["app.apps.example.com"]}]
        with self.assertRaises(RuntimeError):
            sync.run_once(self.conf)
        self.assertEqual(self.writes, [])

    def test_manual_comment_removal_prevents_update(self):
        sync.run_once(self.conf)
        self.writes.clear()
        self.records[0]["comments"] = "manual"
        self.conf["ip"] = "192.0.2.21"
        self.assertEqual(sync.run_once(self.conf)["conflicts"], 1)
        self.assertEqual(self.writes, [])

    def test_reread_refuses_concurrent_record_change(self):
        expected = []
        self.records = [{"name": "app.apps.example.com", "type": "TXT", "disabled": False, "rData": {}}]
        with self.assertRaises(RuntimeError):
            sync.check_unchanged(self.conf, "app.apps.example.com", expected)
        self.assertEqual(self.writes, [])

    def test_queried_record_metadata_does_not_block_update_or_delete(self):
        sync.run_once(self.conf)
        self.queried = True
        self.conf["ip"] = "192.0.2.21"
        self.assertEqual(sync.run_once(self.conf)["updated"], 1)
        self.assertEqual(self.records[0]["rData"]["ipAddress"], "192.0.2.21")
        self.conf.update(deletions=True, allow_empty=True)
        self.hosts = []
        sync.run_once(self.conf)
        state = sync.state_load(self.conf["state_path"])
        state["stale_since"]["app.apps.example.com"] = time.time() - 86401
        sync.state_save(self.conf["state_path"], state)
        self.assertEqual(sync.run_once(self.conf)["deleted"], 1)
        self.assertEqual(self.records, [])

    def test_reread_still_detects_changes_beside_query_metadata(self):
        sync.run_once(self.conf)
        expected = copy.deepcopy(self.records)
        self.records[0]["ttl"] = 999
        self.records[0]["lastUsedOn"] = "changed"
        with self.assertRaises(RuntimeError):
            sync.check_unchanged(self.conf, "app.apps.example.com", expected)

    def test_unmanageable_npm_names_outside_zones_do_not_halt_sync(self):
        self.hosts = [{"enabled": True, "domain_names": ["app.apps.example.com"]},
                      {"enabled": False, "domain_names": ["nas"]},
                      {"enabled": True, "domain_names": ["my_app.lan", "printer"]}]
        self.assertEqual(sync.run_once(self.conf)["added"], 1)
        self.writes.clear()
        for bad in ("bad_name.apps.example.com", "Bad_Name.Apps.Example.Com.", "apps.example.com."):
            self.hosts = [{"enabled": True, "domain_names": ["app.apps.example.com", bad]}]
            if bad == "apps.example.com.":
                self.assertEqual(sync.run_once(self.conf)["added"], 0)  # Valid apex stays excluded.
                continue
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                sync.run_once(self.conf)
        self.assertEqual(self.writes, [])

    def test_redirect_does_not_forward_credentials(self):
        with self.assertRaises(RuntimeError):
            sync.api("GET", self.conf["dns_url"] + "/redirect", headers={"Authorization": "Bearer dummy"})
        self.assertEqual(self.requests, [("/redirect", "Bearer dummy")])

    def test_corrupt_zone_refuses_all_writes(self):
        self.records = [{"name": "app.apps.example.com", "type": "A", "rData": {}}]
        with self.assertRaises(RuntimeError):
            sync.run_once(self.conf)
        self.assertEqual(self.writes, [])

    def test_single_writer_lock(self):
        path = Path(self.temp.name) / "sync.lock"
        with path.open("a+") as first, path.open("a+") as second:
            fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_healthcheck_fresh_stale_and_missing(self):
        env = {"PATH": os.environ.get("PATH", ""), "STATE_FILE": str(self.conf["state_path"]), "SYNC_INTERVAL_SECONDS": "30"}
        def check():
            return subprocess.run([sys.executable, str(Path(sync.__file__)), "--healthcheck"], env=env,
                                  capture_output=True, timeout=5).returncode
        self.assertEqual(check(), 1)
        heartbeat = self.conf["state_path"].parent / "last_success"
        heartbeat.write_text(str(time.time()))
        self.assertEqual(check(), 0)
        heartbeat.write_text(str(time.time() - 1000))
        self.assertEqual(check(), 1)


if __name__ == "__main__":
    unittest.main()
