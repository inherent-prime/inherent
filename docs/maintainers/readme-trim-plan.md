# README Trim Plan

Goal: cut `README.md` from 406 lines to ~120, lead with the inherent.sh value
prop, and surface the trust signals (CI, coverage, release, license) a
maintained open-source repo is expected to show.

## 1. Diagnosis

| Problem | Evidence |
| --- | --- |
| Value prop is buried and off-message | README says "backend for RAG"; inherent.sh says "One brain, every agent. Store context once, stop paying for it everywhere." |
| Same content repeated 3-4 times | About / Why Use It / The Pitch / Key Features all list ingestion, chunking, embeddings, API. File-types link appears 3 times. |
| Trust signals missing | Only 2 badges. Coverage is enforced in CI (82-98% floors) but never shown. No release, license, Python, docs badges. |
| Stale facts | Docs link is malformed (`docs.inherent.sh/](https://...`). Says "two main services"; repo has four (`inh-cli`, `inh-contracts` missing). |
| Docs duplicated into README | Smoke test, bootstrap internals, endpoint table, per-service dev commands, Azure deploy notes all exist in `docs/`. |

## 2. Target structure (top-down, ~120 lines)

1. Hero image + link bar (Website, Docs, Sandbox, Blog)
2. Badge row: CI, Integration, Coverage, Release, License, Python, Docs
3. One-line pitch + 3-sentence "what it is"
4. Why Inherent (4 bullets, from inherent.sh)
5. Quickstart (two paths: `make quickstart` and published images)
6. First search (upload + search, 2 curl calls)
7. Architecture (ASCII diagram, kept)
8. Retrieval quality baseline (generated table, kept; it is a real differentiator)
9. Project health: CI gates, coverage floors, release cadence, docs
10. Repository layout (4 services, one line each)
11. Contributing / Security / Support / License (one line each)

## 3. What gets cut and where it lives

| Cut from README | Now lives in |
| --- | --- |
| About, Why Use It, The Pitch (merged into one "Why Inherent") | README §4 |
| Typical Flow, What's In The Repo, How It Works | Merged into Architecture |
| Ingestion `/ingest` curl example | `services/inh-ingestion-svc/Readme.md` |
| Bootstrap internals (both principals, `SEED_PRINCIPAL_B`) | `docs/getting-started/local.md` |
| Full smoke test script | `docs/getting-started/local.md` |
| Local Endpoints table | `docs/getting-started/local.md` |
| Per-service dev commands | `CONTRIBUTING.md` |
| Published-image notes (arm64, registry override, production hardening) | `docs/deploy/production.md`, `docs/deploy/azure.md` |
| Roadmap / ADR paragraph | One link under Architecture |

Before deleting, verify each target doc already contains the content; move it
if not. Nothing is lost, only relocated.

## 4. Badges to add and the work behind each

| Badge | Source | Work needed |
| --- | --- | --- |
| CI | existing `ci.yml` | none |
| Integration & Eval Gate | existing `integration.yml` | none |
| Coverage | **new** | Add a `coverage-badge` step in `ci.yml` that merges the four `.coverage` files, computes total %, and publishes via `schneegans/dynamic-badges-action` to a gist (needs `GIST_SECRET`). Alternative: Codecov upload + `codecov.io` badge. |
| Release | GitHub releases (`v0.6.0` exists) | none, shields `github/v/release` |
| License | `LICENSE` (MIT) | none |
| Python 3.11+ | static shields badge | none |
| Docs | `docs.yml` workflow | none |
| E2E smoke | existing `e2e-smoke.yml` | optional, badge row gets crowded; keep to 6 |

## 5. Fixes bundled with the trim

- Fix the broken Docs URL. Use `https://docs.inherent.sh/` (mkdocs `site_url` is the GitHub Pages fallback).
- List all four services.
- Add a "Project health" section that states the coverage floors (82 / 84 / 89 / 98) and the ratchet policy in two lines, linking `docs/testing.md`.
- Add `CHANGELOG.md` link and release cadence sentence.

## 6. Execution order

1. Open a GitHub issue "docs: trim README and add trust badges" (AGENTS.md rule).
2. PR 1 (docs-only): replace README with the draft below, relocate cut content, fix links. `Docs` CI check must stay green.
3. PR 2 (CI): add coverage badge job in `ci.yml` and gist secret. Add badge to README once the gist publishes.
4. Verify: badges render on `main`, all README links resolve (`make docs` link check), `docs/README.md` reading order still points at README.

## 7. Draft README

Implemented: the trimmed `README.md` on `main` is the source of truth (#361).
The coverage badge is static until the gist is wired up; see
[coverage-badge.md](coverage-badge.md).
