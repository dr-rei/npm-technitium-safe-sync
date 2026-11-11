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
