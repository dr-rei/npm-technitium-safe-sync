#!/bin/sh
set -eu
image="${1:?image required}"
# The fixture contains dummy values only. Every container has network disabled.
run() {
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 32 --memory 128m \
    --env-file tests/fixtures/smoke.env "$image" "$@"
}
run --check
docker run --rm --network none --read-only --entrypoint python "$image" -c \
  'import os; assert os.getuid() == 10001; assert not os.access("/app/sync.py", os.W_OK)'
# Missing credentials fail without printing their values.
if docker run --rm --network none "$image" --check; then
  echo 'ERROR: missing credentials were accepted'; exit 1
fi
if run --healthcheck; then
  echo 'ERROR: missing heartbeat was healthy'; exit 1
fi
