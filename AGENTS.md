Guidance for working in this repository.

## General Guidance
- Consult the knowledge-graph `graphify-out/` when require context about the repo. If its not there, ask the user to build one.
- Always thinks the end user as an AI agent, so always develop solutions that is performant and cost effective for the end user.
- The Definition of Done is considered when all tests are passing and documentations is updated. 
- Never mark any feature / bug complete unless unit tests are passing and documentation is updated
- Always write internal developer documentation if needed in the `/project/dev/docs` folder and keep it updated
- Read `.memory/index.md` at the start of a session — it carries current state, open threads and known sharp edges. See `.memory/README.md` for what belongs there and what must stay in `CHANGELOG.md` / `docs/adr/` / `docs/developer/learnings.md` instead.
- If there is a major decision or change that has happened in the repository, add a dated one-line entry to `.memory/timeline/<YYYY-MM>.md`, refresh `.memory/index.md` if the current state changed, and update the `./docs` folder
- Before commit always check if the code build, lint, tests and smoke tests are passing in local to save building time in Cloud
- Whenever you commit some code, always make sure you write proper description of the change
- Always raise PRs against `main` — it is the sole protected integration
  branch (GitHub ruleset `main-protect`, id 16976743). `dev` is retired to
  scratch status: no protection, no required flow, not a PR target.
- No commit should be without a Github Issue. If there is no issue or adhoc work, make sure to create an issue with details and then proceed to write the code

## Mandatory Quality Gates (ALL agents must follow)

Every agent — main or subagent — MUST complete these before marking work done or committing:

1. **Unit Tests**: Write unit tests for every new feature or bug fix. No exceptions.
2. **E2E Tests**: Check E2E whenver there is a major change with more than 5 files has changed or its a new feature altogether
3. **Lint + Format Check**: Run linter and formatter before commit.
4. **Sanity Check**: Run full test suite + type check to verify nothing is broken.
5. **Smoke Test**: Always do smoke test in local before pushing to remote branch.
6. **Root-cause CI flakes, don't theorize**: never ship a CI-flake fix on an unverified "prime suspect" — run the diagnostic before committing, and when the bug is a shared-helper timeout (e.g. a DB-clearing hook), grep every call site and fix them all, not just the one that failed

## Branch Policy & Merge Gates

`main` is the only protected branch (ruleset `main-protect`, id 16976743,
`enforcement: active`, pattern `refs/heads/main`, `refs/heads/release*`). It
blocks branch deletion and non-fast-forward pushes, requires linear history,
runs `code_quality` and `copilot_code_review`, and gates merge on a
`pull_request` rule: 0 required approvals (sole-maintainer repo — GitHub
forbids self-approval, so the human gate is green checks + a human clicking
merge), all review threads must be resolved, squash/rebase merge only. `dev`
carries none of this — it is scratch space, not a PR target.

Three lanes, by when they run and what they can block:

| Lane | Runs on | Checks | Blocks |
| --- | --- | --- | --- |
| **PR-blocking** | every PR into `main` | `Required tests before merge` (`ci.yml`: lint, format, mypy, bandit, unit+contract tests, coverage floors — all three services); `E2E smoke` (`e2e-smoke.yml`: boots the Compose stack, runs `-m "smoke and compose"`, 6 tests, 40-min job timeout); `Conventions` (`conventions.yml`: requires a `CHANGELOG.md` entry when `services/**` changes and a `docs/` touch when API routers / the MCP tool registry / shared contracts change — skippable per-PR with the `no-changelog` / `no-docs-needed` labels) | merging the PR |
| **Post-merge** | push to `main`, nightly cron, manual dispatch | `integration.yml`: full Compose suite, the retrieval-eval hard gate (tolerance derived from corpus resolution, #236 — see `docs/testing.md`), search + ingestion benchmarks, dead-letter recovery E2E, baseline ratchet and regression-alert jobs | nothing directly — it reports and files issues against code already on `main` |
| **Release-only** | a final `vX.Y.Z` tag / manual dispatch | `publish.yml` (human-approved GHCR image publish) | cutting/publishing a release |

The three PR-blocking checks above are the required-status-check *intent* for
`main-protect`; their registration on the ruleset is a separate, later step
— until then, treat a red `ci.yml` / `e2e-smoke.yml` / `conventions.yml` on a
PR as blocking in practice even though GitHub isn't yet enforcing it for you.

## Ways of working

1. Find relevant skills in the project which can help achieve the goal better
2. Always use sub-agent driven development for long horizon goals and use skills that are in the system to assist in brainstorming or planning and execution
3. Always use subagents for coding tasks example Sonnet5 for coding while Opus for review and glueing everything and Fable for judgement
4. Use a working file in `.memory` for context handover
5. The core engine which powers multiple applications, whenever you propose a solution think about legacy support. 

# PAST LEARNINGS

These are the instructions based on past learnings and I need you to keep it updated if its required.
While picking selectively pick based on what target we are chasing for now. 

## Release Tagging & Docs

- Every merged PR that changes behavior, API surface, configuration, or
  deployment MUST add a one-line entry under `[Unreleased]` in
  `CHANGELOG.md`, in a Keep a Changelog category (Added / Changed / Fixed /
  Deprecated / Removed / Security), ending with `(#PR, #issue)` refs.
  Docs-only, CI-only, and test-only changes are exempt. Cutting a release =
  renaming `[Unreleased]` to the version — this is how every piece of work
  is tagged to a release and categorized.
- Update the docs a change invalidates (site pages under `docs/`, reference
  pages, examples) in the same PR — the `Docs` CI check must stay green. At
  release time the docs are already current: releasing is rename + tag +
  publish (see [docs/maintainers/releasing.md](docs/maintainers/releasing.md)),
  never a catch-up docs sweep.
- **Naming — bare version, no codenames.** Release titles, git tags, and
  `CHANGELOG.md` version headings are the version and nothing else:
  `v0.5.0` / `## [0.5.0] — 2026-07-13`. Never a marketing codename or theme
  ("Org-readiness program", "Ingestion hardening") — no ` — <name>` suffix,
  ever. The narrative belongs in the release body, not the title.
- **Release body — terse changelog, not prose.** The GitHub Release body is
  the version's `CHANGELOG.md` entry condensed to one-line bullets under Keep
  a Changelog categories (Added / Changed / Fixed / Security / Breaking /
  Upgrade), each ending with `(#PR)`. Lead with a one-line package-versions
  line; close with a link to `CHANGELOG.md`. No paragraph intro, no "TL;DR",
  no emoji beyond `⚠️` on breaking bullets. Write for an agent scanning it.

## Coding Standards

- Follow strict coding standard maximize for explanability to humans
- While designing any solution think in SOLID, DRY, KISS and whatever applicable. 
- Always write tests first and then do the development later
- A feature is only complete when all tests are passed and you can provide proof of complete.
- All the code must have comments, which humans can understand easily with the context of this repo.
- Always keep the docs updated incase there are breaking changes highlight early
- Incase of long tasks always use sub-agents to achieve the goal.

## Defect Prevention

Rules from the #98/#99/#100/#112 retrospective. Apply before closing any task.
Durable lessons behind these rules live in
[docs/developer/learnings.md](docs/developer/learnings.md) — read the matching
entry before touching a related area; add one when a shipped defect teaches
something new.

- **Pattern sweep**: after fixing a bug, grep both services for the same defect
  pattern. State the sweep result (hits or "clean") in the PR description.
- **Dual-surface failure parity**: when touching a capability that exists on
  both REST and MCP (upload, refresh, delete, ...), diff the two handlers'
  failure paths — MQ down, DB down, vector store down, not-found, permission.
  Both surfaces must leave the same document state and surface an error. Pin
  any pair you touch in
  `services/inh-public-api-svc/tests/contract/test_failure_parity.py`.
- **Compensate state mutations**: a state write followed by a publish (or any
  second fallible step) needs a tested compensating mark-failed path. The
  compensation is itself fallible (#99): route it through
  `services/inh-public-api-svc/src/services/compensation.py::mark_document_failed_with_retry`
  — never a bare `mark_document_failed` inside an `except` block.
  Log-and-swallow is acceptable only for observability side-channels (metrics,
  lineage, audit) — never when it leaves persistent state contradicting the
  response.
- **MCP tools**: add new tools only as a `_TOOLS` registry entry in
  `services/inh-public-api-svc/src/mcp_server/server.py` (one entry carries
  schema + permission + handler). Never reintroduce separate permission maps
  or dispatch chains.
- **Surface friction**: if a change requires the same edit in 3+ places, or
  you notice an unfiled defect in code you read but don't change, file a
  GitHub issue before finishing. Don't silently comply or move on.
- **Adversarial pass**: review the diff for swallowed exceptions, failure-path
  asymmetries, and state/response divergence before pushing — tests-green is
  not done.
- **Release visibility**: writing release notes is not the same as publishing
  them (#112). A release is only discoverable when there is a GitHub Release
  on the tag (not just an annotated tag message or a CHANGELOG entry) and the
  GHCR package pages carry a description (`publish.yml` must pass both
  `labels:` and `annotations:` from `metadata-action` — a multi-platform
  build's GHCR page reads annotations, not labels). Check both before calling
  a release done.

## Writing Standards

- Be concise and direct - remove unneccessary adjective and verbose descriptions
- Use active voice - "Creates agent" not "Agent is created".
- For Documentation write like prescription which brief and concise as AI Agents are going to read it. 
