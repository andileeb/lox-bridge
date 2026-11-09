# TrueNAS catalog app (not submitted yet)

`loxbridge/` is the app for the official catalog, ready to become `ix-dev/community/loxbridge` in
[truenas/apps](https://github.com/truenas/apps). Nothing has been submitted. It's tested here by
`.github/workflows/truenas-app.yml`, which runs TrueNAS' own `ci.py` (render with their library, start, wait
for healthy) against a fresh checkout of truenas/apps.

Compared to `compose.yaml` (Install via YAML): TrueNAS creates the config / work / backups datasets itself
(ixVolume) or uses a host path per storage, sets the permissions with its own helper container and starts LoxBridge
directly as `apps` (568). Default port 30568 (free in the catalog's 30000–40000 range on 2026-11-09).

## Submitting (when ready)

1. Open an issue in truenas/apps: "New app: LoxBridge" (they ask for this first to avoid duplicate work).
2. Fork truenas/apps, copy `truenas/loxbridge` to `ix-dev/community/loxbridge`, run
   `python3 .github/scripts/ci.py --train community --app loxbridge --test-file basic-values.yaml` (updates the
   generated metadata in `app.yaml`; needs Linux/amd64, not Rosetta on Apple Silicon).
3. Open a draft PR with their app template. Icon: `loxbridge/web/static/icon.svg`; screenshots: `docs/images/*-light.png`.
   Their reviewer uploads both to the TrueNAS CDN and gives you the URLs for `app.yaml` / `item.yaml`.
4. Versions: the TrueNAS app `version`, `app_version` and the image tag always equal the LoxBridge release
   (`scripts/bump-version.sh`, checked by `release.yml`). Add the image digest (`X.Y.Z@sha256:…`) in the PR; their
   Renovate keeps it current afterwards. Their `ci.py` may bump `version` itself when metadata changes: set it back.

Before that (see PLAN.md): a test restore onto a spare SD card, and a few weeks of real use.
