#!/bin/sh
# Exercise exactly the two fresh-install files, with dummy local-only endpoints.
set -eu
image="${1:?test image required}"
# Optional previously released image: its state must survive upgrading to $image.
previous="${2:-}"
project="sync-quickstart-ci-${GITHUB_RUN_ID:-$$}-${GITHUB_RUN_ATTEMPT:-1}"
fixture="$(mktemp -d)"
export QUICKSTART_LITERAL=SHOULD_NOT_EXPAND
compose() { (cd "$fixture" && docker compose -p "$project" "$@"); }
cleanup() {
  # Only the uniquely named disposable test project is removed, including its volume.
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$fixture"
}
trap cleanup EXIT
cp compose.quickstart.yaml "$fixture/compose.yaml"
python3 - "$fixture/.env" "$image" <<'PY'
from pathlib import Path
import sys
value = Path('.env.quickstart.example').read_text()
value = value.replace(next(line for line in value.splitlines() if line.startswith('SYNC_IMAGE=')), 'SYNC_IMAGE=' + sys.argv[2])
value = value.replace('https://npm.internal.example.com', 'https://127.0.0.1:1')
value = value.replace('https://dns.internal.example.com', 'https://127.0.0.1:1')
value = value.replace("'REPLACE_WITH_NPM_ACCOUNT_PASSWORD'", "' literal-$QUICKSTART_LITERAL ${QUICKSTART_LITERAL} # space and \\' quote '")
value = value.replace("'REPLACE_WITH_ZONE_SCOPED_API_TOKEN'", "'dummy-$QUICKSTART_LITERAL-${QUICKSTART_LITERAL}#token'")
Path(sys.argv[1]).write_text(value)
PY
compose config --quiet
compose config --format json | python3 -c '
import json, sys
s = json.load(sys.stdin)["services"]["npm-technitium-safe-sync"]
assert "build" not in s and "secrets" not in s and "ports" not in s
assert s["user"] == "10001:10001" and s["read_only"] is True
assert s["volumes"][0]["type"] == "volume" and s["volumes"][0]["target"] == "/data"
assert s["environment"]["DRY_RUN"] == "false"
'
if NPM_SECRET= compose config --quiet >/dev/null 2>&1; then
  echo 'ERROR: quickstart accepted an empty password'; exit 1
fi
compose run --rm --no-deps npm-technitium-safe-sync --check
# Starting needs no host mkdir/chown, source tree, extra files, or root container.
# The fixture has loopback closed-port endpoints; API failure here is intentional.
compose up -d
container="$(compose ps -q npm-technitium-safe-sync)"
[ "$(docker inspect --format '{{.State.Running}}' "$container")" = true ]
compose stop
compose run --rm --no-deps --entrypoint python npm-technitium-safe-sync -c '
import os
from pathlib import Path
import sync
assert os.getuid() == 10001 and not os.access("/app/sync.py", os.W_OK)
assert os.environ["NPM_SECRET"] == " literal-$QUICKSTART_LITERAL ${QUICKSTART_LITERAL} # space and '\'' quote "
assert os.environ["TECHNITIUM_TOKEN"] == "dummy-$QUICKSTART_LITERAL-${QUICKSTART_LITERAL}#token"
assert not Path("/data/state.json").exists()
state = {"version": 1, "owned": {"app.apps.example.com": "192.0.2.20"}, "stale_since": {}}
sync.state_save(Path("/data/state.json"), state)
'
# The README backup command must work against the stopped service container.
mkdir "$fixture/state-backup"
compose cp npm-technitium-safe-sync:/data/. "$fixture/state-backup/"
python3 - "$fixture/state-backup/state.json" <<'PY'
from pathlib import Path
import json
import sys
assert json.loads(Path(sys.argv[1]).read_text())["owned"] == {"app.apps.example.com": "192.0.2.20"}
PY
# Ordinary down and recreation must retain ownership, unlike down --volumes.
compose down
compose run --rm --no-deps --entrypoint python npm-technitium-safe-sync -c '
from pathlib import Path
import sync
assert sync.state_load(Path("/data/state.json"))["owned"] == {"app.apps.example.com": "192.0.2.20"}
'
if [ -n "$previous" ]; then
  # Documented upgrade: the previous release owns state, then only SYNC_IMAGE changes.
  compose down
  SYNC_IMAGE="$previous" compose run --rm --no-deps --entrypoint python npm-technitium-safe-sync -c '
from pathlib import Path
import sync
state = {"version": 1, "owned": {"old.apps.example.com": "192.0.2.30"}, "stale_since": {}}
sync.state_save(Path("/data/state.json"), state)
'
  compose up -d --force-recreate
  [ "$(docker inspect --format '{{.Config.Image}}' "$(compose ps -q npm-technitium-safe-sync)")" = "$image" ]
  compose stop
  compose run --rm --no-deps --entrypoint python npm-technitium-safe-sync -c '
from pathlib import Path
import sync
assert sync.state_load(Path("/data/state.json"))["owned"] == {"old.apps.example.com": "192.0.2.30"}
'
  echo 'PASS: state written by the previous release survives the image upgrade'
fi
echo 'PASS: two-file startup, required credentials, literal secrets, nonroot volume initialization and persistent state'
