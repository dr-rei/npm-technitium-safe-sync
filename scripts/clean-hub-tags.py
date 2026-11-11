"""Explicit manual cleanup of development tag aliases in this project's Hub repo."""
import json
import os
import re
import sys
import urllib.error
import urllib.request

BASE = "https://hub.docker.com"
TAGS = BASE + "/v2/repositories/drreilab/npm-technitium-safe-sync/tags/"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("Docker Hub redirect refused")


def call(url, method="GET", payload=None, token=None):
    if not url.startswith(BASE + "/v2/"):
        raise RuntimeError("Unexpected Docker Hub URL")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, method=method, headers=headers,
                                     data=json.dumps(payload).encode() if payload else None)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=30) as r:
            body = r.read(4_000_001)
            if len(body) > 4_000_000:
                raise RuntimeError("Docker Hub response too large")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        exc.close()
        raise RuntimeError("Docker Hub HTTP %d during %s" % (exc.code, method)) from None


def tags():
    result = {}
    url = TAGS + "?page_size=100"
    seen = set()
    while url:
        if not url.startswith(TAGS) or url in seen:
            raise RuntimeError("Invalid tag pagination")
        seen.add(url)
        page = call(url)
        for item in page["results"]:
            name, digest = item["name"], item["digest"]
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest) or name in result:
                raise RuntimeError("Invalid or duplicate tag metadata")
            result[name] = digest
        url = page.get("next")
    return result


def is_extra(name):
    return name == "main" or re.fullmatch(r"sha-[a-f0-9]{40}", name) is not None


def clean(apply=False):
    before = tags()
    protected = {name: digest for name, digest in before.items() if not is_extra(name)}
    if "latest" not in protected or not any(re.fullmatch(r"\d+\.\d+\.\d+", name) for name in protected):
        raise RuntimeError("Expected stable release tags are missing; refusing cleanup")
    extra = sorted(name for name in before if is_extra(name))
    print("Preserve:", ", ".join(sorted(protected)))
    print("Remove aliases:", ", ".join(extra) or "none")
    if not apply or not extra:
        return
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("CLEANUP_CONFIRM") != "delete-extra-tags":
        raise RuntimeError("Deletion requires the explicitly confirmed manual Actions workflow")
    auth = call(BASE + "/v2/auth/token", "POST", {
        "identifier": os.environ["DOCKERHUB_USERNAME"], "secret": os.environ["DOCKERHUB_TOKEN"]})
    token = auth["access_token"]
    if not isinstance(token, str) or not token:
        raise RuntimeError("Docker Hub authentication failed")
    for name in extra:
        # Delete only this allowlisted tag alias, never a manifest or repository.
        call(TAGS + name + "/", "DELETE", token=token)
        print("Removed alias:", name)
    after = tags()
    if any(after.get(name) != digest for name, digest in protected.items()):
        raise RuntimeError("Protected tag digest changed; inspect before any further cleanup")
    if any(is_extra(name) for name in after):
        raise RuntimeError("Extra tags remain after cleanup")
    print("Verified protected tags and their digests unchanged:", ", ".join(sorted(after)))


if __name__ == "__main__":
    try:
        if sys.argv[1:] not in ([], ["--apply"]):
            raise RuntimeError("Use no argument to preview or --apply in the confirmed workflow")
        clean(sys.argv[1:] == ["--apply"])
    except Exception as exc:
        print(str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__, file=sys.stderr)
        sys.exit(1)
