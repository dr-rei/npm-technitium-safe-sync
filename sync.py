#!/usr/bin/env python3
"""Conservative Nginx Proxy Manager -> Technitium DNS A record reconciler.
Only owns A records it created; all other DNS records are left untouched.
"""
import fcntl
import ipaddress
import json
import logging
import math
import os
import re
import sys
import signal
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LOG = logging.getLogger("npm-technitium-safe-sync")
MARKER = "managed-by:npm-technitium-safe-sync/v1"
LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def flag(name, default=False):
    value = os.environ.get(name, str(default)).strip().lower()
    if value not in ("true", "1", "yes", "on", "false", "0", "no", "off"):
        raise ValueError("Invalid boolean: " + name)
    return value in ("true", "1", "yes", "on")


def secret(name):
    if name in os.environ and name + "_FILE" in os.environ:
        raise ValueError("Set only one of " + name + " and " + name + "_FILE")
    value = (Path(os.environ[name + "_FILE"]).read_text("utf-8").rstrip("\r\n")
             if name + "_FILE" in os.environ else os.environ.get(name, ""))
    if not value or not value.strip() or "\r" in value or "\n" in value:
        raise ValueError("Missing or invalid credential: " + name)
    return value


def normalized_name(domain):
    if not isinstance(domain, str):
        return None
    value = domain.strip().rstrip(".").lower()
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or not all(LABEL.fullmatch(x) for x in labels):
        return None
    return value


def validate_config():
    cfg = {
        "npm_url": os.environ["NPM_URL"].rstrip("/"),
        "npm_identity": os.environ["NPM_IDENTITY"],
        "npm_secret": secret("NPM_SECRET"),
        "dns_url": os.environ["TECHNITIUM_URL"].rstrip("/"),
        "dns_token": secret("TECHNITIUM_TOKEN"),
        "tech_auth": os.environ.get("TECHNITIUM_AUTH_MODE", "bearer"),
        "ip": str(ipaddress.IPv4Address(os.environ["NPM_LAN_IP"])),
        "zones": tuple(x.strip().lower().rstrip(".") for x in os.environ["DNS_ZONES"].split(",") if x.strip()),
        "interval": int(os.environ.get("SYNC_INTERVAL_SECONDS", "300")),
        "ttl": int(os.environ.get("DNS_TTL_SECONDS", "120")),
        "dry_run": flag("DRY_RUN", True),
        "deletions": flag("ENABLE_DELETIONS", False),
        "delete_grace": int(os.environ.get("DELETE_GRACE_SECONDS", "86400")),
        "max_deletes": int(os.environ.get("MAX_DELETES_PER_RUN", "5")),
        "allow_empty": flag("ALLOW_EMPTY_NPM", False),
        "state_path": Path(os.environ.get("STATE_FILE", "/data/state.json")),
    }
    for url in (cfg["npm_url"], cfg["dns_url"]):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("URLs require http(s), a hostname, and no credentials/query/fragment")
        if parsed.scheme == "http" and not flag("ALLOW_INSECURE_HTTP"):
            raise ValueError("HTTP sends credentials in plaintext; use HTTPS or explicitly set ALLOW_INSECURE_HTTP=true on a trusted network")
        parsed.port  # Validate the port before sending credentials.
    if not cfg["npm_identity"].strip() or cfg["tech_auth"] not in ("bearer", "parameter"):
        raise ValueError("Invalid identity or TECHNITIUM_AUTH_MODE")
    if not cfg["zones"] or any(normalized_name(z) != z for z in cfg["zones"]):
        raise ValueError("DNS_ZONES must be comma-separated DNS zone names")
    if len(set(cfg["zones"])) != len(cfg["zones"]) or any(a.endswith("." + b) for a in cfg["zones"] for b in cfg["zones"] if a != b):
        raise ValueError("DNS_ZONES must not overlap or repeat")
    if cfg["ttl"] < 30 or cfg["interval"] < 30 or cfg["delete_grace"] < 300 or cfg["max_deletes"] < 0:
        raise ValueError("Invalid interval/TTL/deletion safety limits")
    if cfg["state_path"].parent != Path("/data") and os.environ.get("STATE_FILE") is None:
        raise ValueError("State must be persisted")
    return cfg


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("API redirects are refused; configure the final management URL")


def api(method, url, params=None, payload=None, headers=None):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = None
    headers = dict(headers or {})
    if payload is not None:
        if isinstance(payload, dict) and headers.get("Content-Type") == "application/json":
            data = json.dumps(payload).encode("utf-8")
        else:
            data = urllib.parse.urlencode(payload).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    # Ignore ambient HTTP proxies so management credentials go only to the configured endpoint.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(req, timeout=12) as response:
            body = response.read(4_000_001)
        if len(body) > 4_000_000:
            raise RuntimeError("API response exceeds size limit")
        obj = json.loads(body)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("API HTTP status %d" % exc.code) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise RuntimeError("API transport, TLS, or JSON failure") from None
    if not isinstance(obj, (dict, list)):
        raise RuntimeError("API returned an invalid JSON shape")
    if isinstance(obj, dict) and obj.get("status") in ("error", "invalid-token", "2fa-required"):
        raise RuntimeError("Technitium API rejected request: " + obj["status"])
    return obj


def npm_hosts(cfg):
    auth = api("POST", cfg["npm_url"] + "/api/tokens", None,
               {"identity": cfg["npm_identity"], "secret": cfg["npm_secret"]},
               {"Content-Type": "application/json"})
    token = auth.get("token") if isinstance(auth, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("NPM authentication did not return a token")
    hosts = api("GET", cfg["npm_url"] + "/api/nginx/proxy-hosts",
                headers={"Authorization": "Bearer " + token})
    if not isinstance(hosts, list):
        raise RuntimeError("NPM proxy-host API did not return a list; refusing changes")
    desired = set()
    enabled = 0
    for host in hosts:
        if not isinstance(host, dict) or "enabled" not in host or "domain_names" not in host:
            raise RuntimeError("Malformed NPM host data; refusing changes")
        if type(host["enabled"]) not in (bool, int) or host["enabled"] not in (0, 1):
            raise RuntimeError("Malformed NPM enabled value; refusing changes")
        names = host["domain_names"]
        if not isinstance(names, list):
            raise RuntimeError("Malformed NPM domain_names; refusing changes")
        for raw in names:
            if isinstance(raw, str) and raw.startswith("*.") and normalized_name(raw[2:]):
                continue
            domain = normalized_name(raw)
            if not domain:
                raise RuntimeError("Malformed NPM hostname; refusing changes")
            if host["enabled"] and any(domain.endswith("." + zone) for zone in cfg["zones"]):
                desired.add(domain)
        enabled += int(bool(host["enabled"]))
    if not desired and not cfg["allow_empty"]:
        raise RuntimeError("NPM returned no permitted active hostnames; fail-closed (ALLOW_EMPTY_NPM=false)")
    LOG.info("Read %d NPM hosts, %d enabled, %d permitted unique names", len(hosts), enabled, len(desired))
    return desired


def zone_for(name, zones):
    matching = [z for z in zones if name.endswith("." + z)]
    return max(matching, key=len) if matching else None


def tech_call(cfg, path, params=None, write=False):
    params = dict(params or {})
    headers = {}
    if cfg.get("tech_auth", "bearer") == "parameter":
        params["token"] = cfg["dns_token"]
    else:
        headers["Authorization"] = "Bearer " + cfg["dns_token"]
    result = api("POST", cfg["dns_url"] + path, payload=params, headers=headers)
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise RuntimeError("Unexpected Technitium response")
    return result


def dns_records(cfg):
    records = {}
    for zone in cfg["zones"]:
        obj = tech_call(cfg, "/api/zones/records/get", {"zone": zone, "domain": zone, "listZone": "true"})
        content = obj.get("response", {})
        info = content.get("zone", {})
        if info.get("name", "").rstrip(".").lower() != zone or info.get("type") != "Primary" or info.get("disabled") is not False:
            raise RuntimeError("DNS zone '%s' must be an enabled Primary zone" % zone)
        items = content.get("records")
        if not isinstance(items, list):
            raise RuntimeError("DNS record listing for %s is invalid" % zone)
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError("Malformed Technitium record")
            raw_name = item.get("name")
            if not isinstance(raw_name, str) or not isinstance(item.get("type"), str) or not isinstance(item.get("rData"), dict) or type(item.get("disabled")) is not bool:
                raise RuntimeError("Malformed Technitium record fields")
            name = raw_name.rstrip(".").lower()
            # DNS owners (e.g. _acme-challenge TXT and _sip._tcp SRV) are broader
            # than hostnames. Keep NPM/state validation strict, not the zone listing.
            labels = name.removeprefix("*.").split(".")
            if len(name) > 253 or not all(re.fullmatch(r"[a-z0-9_-]{1,63}", label) for label in labels) or not (name == zone or name.endswith("." + zone)):
                raise RuntimeError("Invalid or out-of-zone Technitium record")
            records.setdefault(name, []).append(item)
    return records


def a_values(records):
    return [r.get("rData", {}).get("ipAddress") for r in records if r.get("type") == "A"]


def can_change(records, old_ip):
    """Never alter a name with multiple records, mixed types or changed IP."""
    return (len(records) == 1 and records[0].get("type") == "A"
            and records[0].get("comments") == MARKER and records[0].get("disabled") is False
            and not records[0].get("expiryTtl", 0) and a_values(records) == [old_ip])


def state_load(path):
    if not path.exists():
        return {"version": 1, "owned": {}, "stale_since": {}}
    state = json.loads(path.read_text("utf-8"))
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("owned"), dict) or not isinstance(state.get("stale_since"), dict):
        raise RuntimeError("Incompatible or corrupt state file; refusing changes")
    try:
        for name, address in state["owned"].items():
            if normalized_name(name) != name or str(ipaddress.IPv4Address(address)) != address:
                raise ValueError()
        for name, since in state["stale_since"].items():
            if name not in state["owned"] or type(since) not in (int, float) or not math.isfinite(since) or since < 0:
                raise ValueError()
    except (ValueError, TypeError):
        raise RuntimeError("Invalid ownership state; refusing changes") from None
    return state


def state_save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def has_blocking_ancestor(domain, zone, snapshot):
    """Apex NS is normal authority; apex aliases still block descendant writes."""
    labels = domain.split(".")
    for i in range(1, len(labels)):
        parent = ".".join(labels[i:])
        for record in snapshot.get(parent, []):
            kind = record.get("type")
            if kind in ("DNAME", "CNAME") or (kind == "NS" and parent != zone):
                return True
        if parent == zone:
            break
    return False


def check_unchanged(cfg, domain, expected):
    """Reduce the read/write race; the upstream API has no conditional-write contract."""
    fresh = dns_records(cfg)
    if fresh.get(domain, []) != expected:
        raise RuntimeError("DNS changed during reconciliation; refusing this write")
    zone = zone_for(domain, cfg["zones"])
    if zone is None:
        raise RuntimeError("DNS name outside permitted zones")
    if has_blocking_ancestor(domain, zone, fresh):
        raise RuntimeError("DNS delegation or alias blocks this write")


def reconcile(cfg, desired, snapshot, state, now=None):
    now = int(time.time() if now is None else now)
    changes = {"added": 0, "updated": 0, "deleted": 0, "conflicts": 0, "pending": 0}

    def commit():
        if not cfg["dry_run"]:
            state_save(cfg["state_path"], state)

    # Validated source presence ends absence, even when DNS conflicts or later
    # writes fail. Persist this before mutations so a restart cannot revive it.
    reset_stale = any(domain in state["stale_since"] for domain in desired)
    for domain in desired:
        state["stale_since"].pop(domain, None)
    if reset_stale:
        commit()

    for domain in sorted(desired):
        zone = zone_for(domain, cfg["zones"])
        if zone is None:
            raise RuntimeError("Desired name outside permitted zones")
        if has_blocking_ancestor(domain, zone, snapshot):
            changes["conflicts"] += 1
            LOG.warning("SKIP name below delegation or alias: %s", domain)
            continue
        existing = snapshot.get(domain, [])
        owned_ip = state["owned"].get(domain)
        if owned_ip is None:
            if existing:
                changes["conflicts"] += 1
                LOG.warning("SKIP unmanaged existing DNS name %s (manual DNS preserved)", domain)
                continue
            LOG.info("%s ADD %s A %s", "PLAN" if cfg["dry_run"] else "APPLY", domain, cfg["ip"])
            if not cfg["dry_run"]:
                check_unchanged(cfg, domain, existing)
                tech_call(cfg, "/api/zones/records/add", {
                    "domain": domain, "zone": zone, "type": "A", "ipAddress": cfg["ip"],
                    "ttl": cfg["ttl"], "comments": MARKER, "overwrite": "false",
                    "ptr": "false", "updateSvcbHints": "false"}, write=True)
                state["owned"][domain] = cfg["ip"]
                commit()
            changes["added"] += 1
            continue
        if not can_change(existing, owned_ip):
            changes["conflicts"] += 1
            LOG.warning("SKIP managed %s: DNS changed or disappeared outside this service", domain)
            continue
        if owned_ip != cfg["ip"] or existing[0].get("ttl") != cfg["ttl"]:
            LOG.info("%s UPDATE %s A %s -> %s TTL %d", "PLAN" if cfg["dry_run"] else "APPLY", domain, owned_ip, cfg["ip"], cfg["ttl"])
            if not cfg["dry_run"]:
                check_unchanged(cfg, domain, existing)
                tech_call(cfg, "/api/zones/records/update", {
                    "domain": domain, "zone": zone, "type": "A", "ipAddress": owned_ip,
                    "newIpAddress": cfg["ip"], "ttl": cfg["ttl"], "comments": MARKER,
                    "ptr": "false", "updateSvcbHints": "false"}, write=True)
                state["owned"][domain] = cfg["ip"]
                commit()
            changes["updated"] += 1
    if cfg["deletions"]:
        stale = sorted(set(state["owned"]) - desired)
        if len(stale) > cfg["max_deletes"]:
            LOG.error("Deletion cap exceeded (%d > %d); no deletions this cycle", len(stale), cfg["max_deletes"])
        else:
            for domain in stale:
                since = state["stale_since"].setdefault(domain, now)
                if now - since < cfg["delete_grace"]:
                    changes["pending"] += 1
                    continue
                target_zone = zone_for(domain, cfg["zones"])
                if target_zone is None:
                    changes["conflicts"] += 1
                    LOG.warning("SKIP delete %s: outside current DNS_ZONES", domain)
                    continue
                old_ip = state["owned"][domain]
                existing = snapshot.get(domain, [])
                if not can_change(existing, old_ip):
                    changes["conflicts"] += 1
                    LOG.warning("SKIP delete %s: record no longer matches owned state", domain)
                    continue
                LOG.warning("%s DELETE %s A %s", "PLAN" if cfg["dry_run"] else "APPLY", domain, old_ip)
                if not cfg["dry_run"]:
                    check_unchanged(cfg, domain, existing)
                    tech_call(cfg, "/api/zones/records/delete", {"domain": domain, "zone": target_zone,
                                                                  "type": "A", "ipAddress": old_ip}, write=True)
                    del state["owned"][domain]
                    del state["stale_since"][domain]
                    commit()
                changes["deleted"] += 1
    commit()
    return changes


def run_once(cfg):
    desired = npm_hosts(cfg)  # Read & validate entire source BEFORE any write
    snapshot = dns_records(cfg)  # Read & validate every zone BEFORE any write
    state = state_load(cfg["state_path"])
    result = reconcile(cfg, desired, snapshot, state)
    LOG.info("Cycle completed: %s", json.dumps(result, sort_keys=True))
    (cfg["state_path"].parent / "last_success").write_text(str(int(time.time())))
    return result


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    if sys.argv[1:] == ["--healthcheck"]:
        path = Path(os.environ.get("STATE_FILE", "/data/state.json")).parent / "last_success"
        try:
            age = time.time() - float(path.read_text())
            return 0 if 0 <= age <= max(120, 3 * int(os.environ.get("SYNC_INTERVAL_SECONDS", "300"))) else 1
        except (OSError, ValueError):
            return 1
    if sys.argv[1:] not in ([], ["--check"], ["--once"]):
        raise ValueError("Supported arguments: --check, --once, --healthcheck")
    cfg = validate_config()
    if sys.argv[1:] == ["--check"]:
        LOG.info("Configuration valid; no API requests made")
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    cfg["state_path"].parent.mkdir(parents=True, exist_ok=True)
    with open(cfg["state_path"].parent / "sync.lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if sys.argv[1:] == ["--once"]:
            run_once(cfg)
            return 0
        while not stop.is_set():
            try:
                run_once(cfg)
            except Exception as exc:
                LOG.error("Cycle FAILED (%s); inspect API connectivity, schema, permissions and state. Earlier writes may have completed.", type(exc).__name__)
            stop.wait(cfg["interval"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        LOG.critical("Startup failed (%s); check required settings and state volume permissions", type(exc).__name__)
        sys.exit(1)
