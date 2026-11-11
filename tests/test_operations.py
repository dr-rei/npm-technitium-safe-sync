"""Failure and boundary regressions for the reconciliation process."""
import copy
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import sync
from test_sync import cfg


def managed(ip="192.0.2.20"):
    return {"type": "A", "disabled": False, "comments": sync.MARKER,
            "ttl": 120, "rData": {"ipAddress": ip}}


class OperationTests(unittest.TestCase):
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
