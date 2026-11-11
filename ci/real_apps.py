"""Disposable real NPM/Technitium compatibility checks; never use production data."""
import json
import os
from pathlib import Path
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

sys.path.insert(0, "/app")
import sync

NPM = "http://npm:81"
DNS = "http://dns:5380"
PASSWORD = "Disposable-CI-only-987!"
ADMIN_EMAIL = "admin@sync.example.test"
READER_EMAIL = "reader@sync.example.test"
CHECKS = []


def check(condition, description):
    if not condition:
        raise AssertionError(description)
    CHECKS.append(description)
    print("PASS:", description, flush=True)


def request(base, path, data=None, token=None, method=None, form=False):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        body = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
    req = urllib.request.Request(base + path, data=body, headers=headers,
                                 method=method or ("POST" if data is not None else "GET"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=12) as response:
        value = json.load(response)
    if isinstance(value, dict) and value.get("status") not in (None, "ok"):
        raise AssertionError("Disposable API rejected " + path + ": " + str(value.get("errorMessage", value["status"])))
    return value


def ready(call):
    deadline = time.monotonic() + 180
    while True:
        try:
            return call()
        except (OSError, ValueError, AssertionError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def dns_answers(domain):
    labels = b"".join(bytes([len(label)]) + label.encode() for label in domain.split(".")) + b"\0"
    packet = struct.pack("!6H", 12345, 0x0100, 1, 0, 0, 0) + labels + struct.pack("!HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        sock.sendto(packet, ("dns", 53))
        data = sock.recv(65535)
    ident, flags, questions, answers, _, _ = struct.unpack("!6H", data[:12])
    assert ident == 12345 and flags & 0x8000
    def skip_name(offset):
        while data[offset]:
            if data[offset] & 0xC0 == 0xC0:
                return offset + 2
            offset += data[offset] + 1
        return offset + 1
    offset = 12
    for _ in range(questions):
        offset = skip_name(offset) + 4
    values = []
    for _ in range(answers):
        offset = skip_name(offset)
        kind, cls, ttl, length = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        if kind == 1 and cls == 1 and length == 4:
            values.append(socket.inet_ntoa(data[offset:offset + 4]))
        offset += length
    return values


def exercise(mode, npm_admin, dns_admin):
    zone = mode + ".sync.example.test"
    outside_zone = "private-" + mode + ".sync.example.test"
    app, alias, manual, keeper = [name + "." + zone for name in ("app", "alias", "manual", "keeper")]
    reader = "reader-" + mode
    def admin_dns(path, data):
        return request(DNS, path, data, dns_admin, form=True)
    for name in (zone, outside_zone):
        admin_dns("/api/zones/create", {"zone": name, "type": "Primary"})
    admin_dns("/api/admin/users/create", {"user": reader, "pass": PASSWORD, "displayName": "CI sync reader"})
    admin_dns("/api/zones/permissions/set", {"zone": zone,
              "userPermissions": "admin|true|true|true|" + reader + "|true|true|true"})
    dns_token = request(DNS, "/api/user/createToken",
                        {"user": reader, "pass": PASSWORD, "tokenName": "disposable-ci"}, form=True)["token"]
    conf = dict(npm_url=NPM, npm_identity=READER_EMAIL, npm_secret=PASSWORD,
                dns_url=DNS, dns_token=dns_token, tech_auth=mode,
                ip=socket.gethostbyname("npm"), zones=(zone,), interval=300, ttl=120,
                dry_run=True, deletions=False, delete_grace=300, max_deletes=5,
                allow_empty=False, state_path=Path("/data") / mode / "state.json")
    conf["state_path"].parent.mkdir(parents=True)
    def add_record(domain, kind, **values):
        admin_dns("/api/zones/records/add", dict(domain=domain, zone=zone, type=kind, ttl=120, **values))
    add_record(manual, "CNAME", cname="elsewhere.example.test")
    add_record("_acme-challenge." + zone, "TXT", text="disposable-manual-txt")
    hosts = [app, alias, manual, "*." + zone, zone, "outside-" + mode + ".example.test"]
    proxy = request(NPM, "/api/nginx/proxy-hosts", {
        "domain_names": hosts, "forward_scheme": "http", "forward_host": "backend",
        "forward_port": 8080, "certificate_id": 0, "access_list_id": 0}, npm_admin)
    result = sync.run_once(conf)
    check(result["added"] == 2 and result["conflicts"] == 1, mode + ": dry-run with real read-only NPM / zone-scoped DNS accounts")
    check(not conf["state_path"].exists() and not dns_answers(app), mode + ": dry-run leaves state and DNS unchanged")
    conf["dry_run"] = False
    check(sync.run_once(conf)["added"] == 2, mode + ": create permitted A records using actual API")
    check(dns_answers(app) == [conf["ip"]] and dns_answers(alias) == [conf["ip"]], mode + ": real authoritative DNS answers")
    check(sync.run_once(conf)["added"] == 0, mode + ": restart/state reload is idempotent")
    req = urllib.request.Request("http://" + conf["ip"] + "/", headers={"Host": app})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as r:
        check("npm-technitium-integration-ok" in r.read().decode(), mode + ": DNS -> NPM -> HTTP backend forwarding")
    conf["ip"] = "192.0.2.21"
    conf["ttl"] = 300
    check(sync.run_once(conf)["updated"] == 2, mode + ": update previous IP and TTL")
    snapshot = sync.dns_records(conf)
    check(sync.a_values(snapshot[app]) == ["192.0.2.21"] and snapshot[app][0]["ttl"] == 300,
          mode + ": actual update parameters and returned record metadata")
    add_record(alias, "AAAA", ipAddress="2001:db8::1")
    conf["ip"] = "192.0.2.22"
    check(sync.run_once(conf)["conflicts"] == 2, mode + ": preserve mixed A/AAAA and manual CNAME")
    admin_dns("/api/zones/records/delete", {"zone": zone, "domain": alias, "type": "AAAA", "ipAddress": "2001:db8::1"})
    admin_dns("/api/zones/records/update", {"zone": zone, "domain": app, "type": "A",
              "ipAddress": "192.0.2.22", "newIpAddress": "192.0.2.22", "ttl": 300, "comments": "manual reclaim"})
    conf["ip"] = "192.0.2.23"
    check(sync.run_once(conf)["conflicts"] == 2, mode + ": preserve manually reclaimed ownership comment")
    # Restore the marker with the API's actual previous IP; this is fixture setup.
    admin_dns("/api/zones/records/update", {"zone": zone, "domain": app, "type": "A",
              "ipAddress": "192.0.2.22", "newIpAddress": "192.0.2.22", "ttl": 300, "comments": sync.MARKER})
    sync.run_once(conf)
    try:
        sync.tech_call(conf, "/api/zones/records/get", {"zone": outside_zone, "domain": outside_zone, "listZone": "true"})
    except RuntimeError:
        check(True, mode + ": scoped token refuses another zone")
    else:
        raise AssertionError("Restricted token unexpectedly read an unrelated zone")
    request(NPM, "/api/nginx/proxy-hosts/" + str(proxy["id"]) + "/disable", {}, npm_admin)
    before = conf["state_path"].read_bytes()
    try:
        sync.run_once(conf)
    except RuntimeError:
        check(conf["state_path"].read_bytes() == before, mode + ": empty enabled source fails closed")
    else:
        raise AssertionError("Empty source accepted without explicit opt-in")
    keep_proxy = request(NPM, "/api/nginx/proxy-hosts", {
        "domain_names": [keeper], "forward_scheme": "http", "forward_host": "backend",
        "forward_port": 8080, "certificate_id": 0, "access_list_id": 0}, npm_admin)
    conf["deletions"] = True
    check(sync.run_once(conf)["pending"] == 2, mode + ": disabled NPM host starts grace, another host retained")
    state = sync.state_load(conf["state_path"])
    for name in (app, alias):
        state["stale_since"][name] = int(time.time()) - 301
    sync.state_save(conf["state_path"], state)
    check(sync.run_once(conf)["deleted"] == 2, mode + ": opt-in expired-grace deletion through actual API")
    check(not dns_answers(app) and dns_answers(keeper) == [conf["ip"]], mode + ": deleted names stop resolving and active name remains")
    snapshot = sync.dns_records(conf)
    check(snapshot[manual][0]["type"] == "CNAME" and "_acme-challenge." + zone in snapshot,
          mode + ": unrelated manual DNS remains intact after deletion")
    # Keep setup account only for fixture operations, never for reconciliation.
    check(keep_proxy["id"] != proxy["id"], mode + ": independent active source host exercised")


def main():
    if os.environ.get("CI_DISPOSABLE_APPS") != "1":
        raise SystemExit("Run only with the disposable internal-network CI application fixtures")
    npm_admin = ready(lambda: request(NPM, "/api/tokens", {"identity": ADMIN_EMAIL, "secret": PASSWORD}))["token"]
    dns_admin = ready(lambda: request(DNS, "/api/user/createToken", {
        "user": "admin", "pass": PASSWORD, "tokenName": "disposable-ci-admin"}, form=True))["token"]
    user = request(NPM, "/api/users", {"name": "CI Reader", "nickname": "Reader", "email": READER_EMAIL,
                   "roles": [], "auth": {"type": "password", "secret": PASSWORD}}, npm_admin)
    request(NPM, "/api/users/" + str(user["id"]) + "/permissions", {
        "visibility": "all", "proxy_hosts": "view", "redirection_hosts": "hidden",
        "dead_hosts": "hidden", "streams": "hidden", "access_lists": "hidden", "certificates": "hidden"}, npm_admin, method="PUT")
    for mode in ("bearer", "parameter"):
        exercise(mode, npm_admin, dns_admin)
    print(json.dumps({"npm": "2.16.0", "technitium": "15.6.2", "checks_passed": len(CHECKS),
                      "scope": "disposable real applications; no production credentials or endpoints"}), flush=True)


if __name__ == "__main__":
    main()
