---
search:
  exclude: true
---

# Releasing

This repository does not assume an automated release train.

## Versioning

- Service versions live in each service `pyproject.toml`.
- Bump versions only when the public behavior, packaging surface, or documented release unit changes in a meaningful way.
- Keep version changes scoped to the service that actually changed unless the whole repository release story changes.

## Release Checklist

0. Bump the `version` in each service `pyproject.toml` whose behavior or
   packaging surface changed this cycle, then regenerate that service's
   `uv.lock` (`uv lock --project services/<svc>`) — the lock pins the version
   and the images build from it (#226). Leave a service alone if nothing in
   it changed, or if it was already bumped by the PR that changed it. This is
   about **package** versions; the published image tag is the repository-level
   git tag and stays decoupled (see
   [Image tags vs. service versions](#image-tags-vs-service-versions)).
1. Confirm README and service docs match the shipped behavior.
2. Run the offline release-acceptance suites in one shot:
   ```bash
   make release-check
   ```
   This runs `make check` plus the public-api `contract` + `security` suites and
   the ingestion `eval` + `failure_injection` suites. The slow Compose e2e gate
   is **not** part of this target — it runs in CI via `integration.yml` (or
   locally via `make dev` + `make test-integration`).
3. Confirm the latest `integration.yml` (Compose e2e gate) and coverage floors
   are green in CI.
4. Cut the changelog: rename `[Unreleased]` in `CHANGELOG.md` to
   `[X.Y.Z] — YYYY-MM-DD` (bare version + date — no codename or theme) and
   add a fresh empty `[Unreleased]` above it. Thanks to the CLAUDE.md
   release-tagging rule, every shipped change is already listed — do not
   reconstruct history at release time.
5. Tag from a clean commit history that does not include unpublished or private planning artifacts (`docs/superpowers/` specs and plans are public by policy).
6. Publish the GitHub Release after pushing the final tag:
   ```bash
   gh release create vX.Y.Z --verify-tag \
     --title "vX.Y.Z" \
     --notes-file <notes.md>
   ```
   The title is the bare tag — no codename, no theme, no ` — <name>` suffix.
   The notes body is the changelog section condensed to one-line bullets:
   lead with a package-versions line, then Added/Changed/Fixed/Security/
   Breaking/Upgrade category headings (each bullet ending `(#PR)`), and close
   with a link to `CHANGELOG.md`. No prose intro, no TL;DR, no emoji beyond
   `⚠️` on breaking bullets — an agent should be able to scan it. `-rcN` tags
   get `--prerelease`.
7. Verify the `Docs` workflow deployed green on `main` and the site's
   Release Notes page shows the new version.

The full set of gating suites, coverage floors, and the README-claim → test
mapping is in the
[release acceptance matrix](release_acceptance_matrix.md).

## Publishing Images

The two custom services are published to the GitHub Container Registry so users
can run the stack without building:

- `ghcr.io/inherent-prime/ingestion-svc`
- `ghcr.io/inherent-prime/public-api-svc`

The other eight services in the stack are upstream OSS images and are **not**
republished — `docker-compose.release.yml` pulls them from their own public
registries. Consumers run the stack with that file (see the README
"Run from published images" section).

### Image tags vs. service versions

The published **image tag is a repository-level release version** taken from the
pushed git tag (`vX.Y.Z`). It is intentionally decoupled from the per-service
`pyproject.toml` versions, because one release publishes **both** images under a
single coordinated tag and `docker-compose.release.yml` selects them with one
`INHERENT_VERSION`. The service `pyproject.toml` versions remain independent
package versions and do not have to match the release tag.

### Approval gate (required, one-time setup)

`.github/workflows/publish.yml` builds both images on a `v*` tag, then the push
job is bound to the **`release-publish`** GitHub Environment. To make publishing
require human sign-off, configure that environment once in
**Settings → Environments → `release-publish` → Required reviewers** (add the
maintainers who may approve a publish). Until reviewers are configured the push
job runs without pausing.

### Cutting an image release

1. Complete the [Release Checklist](#release-checklist) above and merge the
   release commit to `main`.
2. Push a release-candidate tag, let CI build, then approve to publish:
   ```bash
   git tag vX.Y.Z-rc1 && git push origin vX.Y.Z-rc1   # candidate
   ```
   A `-rcN` tag publishes `:X.Y.Z-rcN` only — it never moves `:latest`.
3. When the candidate is accepted, push the final tag:
   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z           # final
   ```
   A final (non-`rc`) tag publishes `:X.Y.Z`, `:X.Y`, and moves `:latest`.
4. In both cases, the workflow pauses on the `release-publish` environment until
   a reviewer approves the run in the **Actions** tab. Nothing is pushed to GHCR
   without that approval.
5. **No automated post-publish e2e runs.** Hetzner e2e was removed — it had not
   produced a genuine pass since 2026-07-13 and its skip path reported success
   while doing nothing, so v0.6.0 shipped believing it had coverage it never
   had. End-to-end testing now lives entirely in `integration.yml` (GitHub
   Actions, full Compose stack on the runner), which gates every merge to
   `main` — so the release commit is already e2e-tested **as source**.
6. **What that leaves unverified, and what to do about it.** `integration.yml`
   builds from source with `docker-compose.yml`. It does not exercise the
   published GHCR images, `docker-compose.release.yml` (localhost-bound
   datastores, Weaviate API-key auth, required-secret guards), or any real VM.
   Before announcing a release, run the published-image smoke check below at
   minimum; for a release with deploy-shaped changes, do a manual VM pass with
   [getting-started/local-vm-test.md](../getting-started/local-vm-test.md).
7. **Publish a GitHub Release from the final tag** (Releases → Draft a new
   release → pick `vX.Y.Z`). Title it exactly `vX.Y.Z` — bare tag, no theme
   or codename, matching step 6 of the
   [Release Checklist](#release-checklist) and the published v0.4.1/v0.5.0
   releases. A theme belongs in the annotated **tag message**, not the
   release title. Use the release-notes format from step 6, and link the
   matching `CHANGELOG.md` entry. Writing notes into
   the tag message or CHANGELOG alone does not make them visible — the
   Releases tab is where consumers actually look, and the tagged git object
   and GHCR package page do not surface either on their own (#112).

`make release-images` prints these steps.

### Published-image parity

Local `act`, the manual VM path, and anyone running
`docker-compose.release.yml` all pull the **published**
`ghcr.io/inherent-prime/public-api-svc:${INHERENT_VERSION:-latest}` — not
workspace source. With Hetzner e2e removed, the smoke check below is the
only automated-ish verification that what shipped matches what CI tested, so
treat it as required on every release rather than only before an `act` run.

If Weaviate has API-key auth enabled (release compose) but the image’s
`SearchService` does not send `Authorization: Bearer`, compose e2e fails with
public-api 500 / Weaviate 401. See
[`docs/audit/act-hetzner-e2e-weaviate-401.md`](../audit/act-hetzner-e2e-weaviate-401.md).

**Republish:** run workflow **Publish images** via `workflow_dispatch` (or push
a `v*` tag). Publish requires `release-publish` environment approval. Prefer
also republishing `ingestion-svc` in the same workflow run (matrix already
builds both).

**Smoke (required on every release, and before re-running act):**

```bash
docker pull ghcr.io/inherent-prime/public-api-svc:X.Y.Z
docker run --rm --entrypoint grep \
  ghcr.io/inherent-prime/public-api-svc:X.Y.Z \
  -n 'Bearer {self._api_key}' \
  /app/services/inh-public-api-svc/src/services/search.py
```

Expect a matching line. No match → do not announce the release; republish from
current `main` first. Run it against the exact tag you published, not
`:latest` — a final tag moves `:latest`, but an `-rcN` does not, so `:latest`
can silently be a different build than the one under test.

Worth also confirming the bumped versions actually landed inside the image,
since `uv.lock` pins them and the images build from the lock (#226):

```bash
docker run --rm --entrypoint grep \
  ghcr.io/inherent-prime/ingestion-svc:X.Y.Z \
  -m1 -n '^version' /app/services/inh-ingestion-svc/pyproject.toml
```

## Documentation Rule

Do not publish a release if the root README or service READMEs describe endpoints, ports, or workflows that the repository does not currently support.
