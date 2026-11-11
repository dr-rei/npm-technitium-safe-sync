# Docker Hub distribution

Configured destination: `docker.io/drreilab/npm-technitium-safe-sync`.

This is a publishing destination, not proof that an image exists. Check successful Actions publication and anonymous pull results before using an image. The existing GHCR release remains available independently of Docker Hub setup.

## One-time setup

1. The namespace owner creates a **public** repository named `npm-technitium-safe-sync` under `drreilab` on Docker Hub. Confirm ownership and visibility; do not overwrite another repository.
2. Create a Docker Hub publishing token with push permission. Do not paste it into chat, source code, .env or workflow YAML.
3. In this GitHub repository, add the token as Actions secret `DOCKERHUB_TOKEN`. Set Actions variable `DOCKERHUB_USERNAME=drreilab` (the publishing login account).
4. Set Actions variable `DOCKERHUB_ENABLED=true` only after the repository and token are ready. The accepted activation value is lowercase `true`; otherwise Docker Hub publishing is disabled. Login and verification follow the image-selection output, so they cannot run without Hub tags.
5. Run **Verify and publish** manually on main, or push a new main commit. The existing verify job runs first. The publish job logs into each enabled registry, builds amd64/arm64 once and pushes the same build to both destinations.
6. Inspect the run result and its digest summary. Docker Hub checks require digest inspection, native/ARM64 startup, Compose pull, and an anonymous pull with an empty Docker credential configuration. A private/unpublished image fails the public-distribution check.

When Docker Hub publishing is disabled, neither its login nor its tags/verification runs; missing Docker Hub credentials do not block GHCR. When enabled, an invalid/missing token or inaccessible destination fails publication verification. A multi-registry push is not atomic: if one destination succeeds and another fails, inspect both before retrying or advertising a version.

## Tags and deployment

Main publishes `drreilab/npm-technitium-safe-sync:main` plus `:sha-<full-commit>`. A newly pushed `vMAJOR.MINOR.PATCH` tag publishes `:MAJOR.MINOR.PATCH` plus the commit tag to both enabled registries. Existing releases are not automatically copied or retagged. Do not move an existing version tag merely to change publishing setup; use a new version for the next release.

After a verified publication:

```sh
docker pull drreilab/npm-technitium-safe-sync:main
```

Use a verified version or registry digest for production, rather than a moving main tag. Set `SYNC_IMAGE=docker.io/drreilab/npm-technitium-safe-sync@sha256:<verified-digest>` in the existing deployment .env; keep endpoints, file secrets and state unchanged. Then run `docker compose pull` and `docker compose up -d --force-recreate` after the normal backup/staging review.

Public consumers do not need a publishing token or Docker login. Runtime NPM/Technitium credentials are unrelated to registry publishing credentials and remain in local file secrets. This is a project-maintained image, not a Docker Official Image badge.

To suspend Docker Hub publishing, set DOCKERHUB_ENABLED=false. Revoke/rotate its token separately when needed. This does not delete an already published image or change existing GHCR images.
