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
                    return self.respond({"status": "ok", "response": {
                        "zone": {"name": "apps.example.com", "type": "Primary", "disabled": False},
                        "records": fixture.records}})
                fixture.writes.append((self.path, params))
                if self.path.endswith("/add"):
                    fixture.records.append({"name": params["domain"], "type": "A", "disabled": False,
                                            "comments": params["comments"], "ttl": int(params["ttl"]),
                                            "rData": {"ipAddress": params["ipAddress"]}})
                elif self.path.endswith("/update"):
                    record = next(r for r in fixture.records if r["name"] == params["domain"])
                    fixture.assertEqual(record["rData"]["ipAddress"], params["ipAddress"])
                    record["rData"]["ipAddress"] = params["newIpAddress"]
                    record["ttl"] = int(params["ttl"])
                elif self.path.endswith("/delete"):
                    fixture.records[:] = [r for r in fixture.records if r["name"] != params["domain"]]
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
        for blocker in ("NS", "DNAME", "CNAME"):
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
