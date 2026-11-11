#!/usr/bin/env bash
# Mirror a published stable GitHub release without rebuilding its image.
set -euo pipefail
umask 077

: "${REPOSITORY:?GitHub repository is required}"
: "${RELEASE_TAG:?Release tag is required}"
: "${RUNNER_TEMP:?Runner temporary directory is required}"
: "${DOCKERHUB_USERNAME:?Publishing username is required}"
: "${DOCKERHUB_TOKEN:?Publishing token is required}"
[[ "$REPOSITORY" == dr-rei/npm-technitium-safe-sync ]] || { echo 'Unexpected source repository'; exit 1; }
[[ "$RELEASE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo 'Use a stable vMAJOR.MINOR.PATCH release'; exit 1; }

version="${RELEASE_TAG#v}"
source_image="ghcr.io/$REPOSITORY"
destination="docker.io/drreilab/npm-technitium-safe-sync"
auth_file="$RUNNER_TEMP/release-registry-auth.json"
trap 'rm -f "$auth_file"' EXIT

gh api "repos/$REPOSITORY/releases/tags/$RELEASE_TAG" > "$RUNNER_TEMP/release.json"
jq -e --arg tag "$RELEASE_TAG" '.tag_name == $tag and .draft == false and .prerelease == false' "$RUNNER_TEMP/release.json" > /dev/null
revision="$(gh api "repos/$REPOSITORY/commits/$RELEASE_TAG" --jq .sha)"
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || { echo 'Invalid release revision'; exit 1; }

# Publishing a GitHub release may overlap its tag build. Wait a bounded time.
source_digest=""
for attempt in {1..20}; do
  if source_digest="$(skopeo inspect --no-creds --format '{{.Digest}}' "docker://$source_image:$version")"; then
    break
  fi
  if [ "$attempt" -eq 20 ]; then echo 'Release image is unavailable; finish its tag build, then retry'; exit 1; fi
  sleep 15
done
[[ "$source_digest" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo 'Invalid source image digest'; exit 1; }
source_ref="$source_image@$source_digest"
skopeo inspect --no-creds "docker://$source_ref" > "$RUNNER_TEMP/source-image.json"
jq -e --arg revision "$revision" --arg source "https://github.com/$REPOSITORY" \
  '.Labels["org.opencontainers.image.revision"] == $revision and .Labels["org.opencontainers.image.source"] == $source' \
  "$RUNNER_TEMP/source-image.json" > /dev/null

curl --fail --silent --show-error \
  'https://hub.docker.com/v2/repositories/drreilab/npm-technitium-safe-sync/' > "$RUNNER_TEMP/hub-repository.json"
jq -e '.is_private == false and .namespace == "drreilab" and .name == "npm-technitium-safe-sync"' "$RUNNER_TEMP/hub-repository.json" > /dev/null

printf '%s' "$DOCKERHUB_TOKEN" | skopeo login --authfile "$auth_file" --username "$DOCKERHUB_USERNAME" --password-stdin docker.io

# Refuse to replace an existing version whose content differs. Other HTTP errors
# are failures, not evidence that a destination is absent.
status="$(curl --silent --show-error --output "$RUNNER_TEMP/hub-tag.json" --write-out '%{http_code}' \
  "https://hub.docker.com/v2/repositories/drreilab/npm-technitium-safe-sync/tags/$version/")"
case "$status" in
  200)
    existing="$(skopeo inspect --no-creds --format '{{.Digest}}' "docker://$destination:$version")"
    [ "$existing" = "$source_digest" ] || { echo 'Existing Docker Hub version has different content; refusing overwrite'; exit 1; }
    echo "Version $version already matches the released digest"
    ;;
  404)
    skopeo copy --all --preserve-digests --authfile "$auth_file" "docker://$source_ref" "docker://$destination:$version"
    ;;
  *) echo "Cannot determine Docker Hub version status (HTTP $status); refusing publication"; exit 1 ;;
esac

actual="$(skopeo inspect --no-creds --format '{{.Digest}}' "docker://$destination:$version")"
[ "$actual" = "$source_digest" ] || { echo 'Published version digest mismatch'; exit 1; }

# An older release backfill must never move latest backwards. The workflow
# serializes these operations and consults GitHub's current Latest designation.
latest_tag="$(gh api "repos/$REPOSITORY/releases/latest" --jq .tag_name)"
if [ "$latest_tag" = "$RELEASE_TAG" ]; then
  skopeo copy --all --preserve-digests --authfile "$auth_file" "docker://$source_ref" "docker://$destination:latest"
  latest_digest="$(skopeo inspect --no-creds --format '{{.Digest}}' "docker://$destination:latest")"
  [ "$latest_digest" = "$source_digest" ] || { echo 'Latest digest mismatch'; exit 1; }
  echo "latest now matches GitHub Latest release $RELEASE_TAG"
else
  echo "Historical release $RELEASE_TAG copied; latest remains tied to $latest_tag"
fi

printf 'Verified release image: `%s:%s`\n\nImmutable image: `%s@%s`\n' "$destination" "$version" "$destination" "$source_digest" >> "$GITHUB_STEP_SUMMARY"
printf '%s\n' "$source_digest" > "$RUNNER_TEMP/verified-release-digest"
