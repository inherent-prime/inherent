## General Guidance
- Consult the knowledge-graph `graphify-out/` when require context about the repo. If its not there, ask the user to build one.
- Always thinks the end user as an AI agent, so always develop solutions that is performant and cost effective for the end user.
- The Definition of Done is considered when all tests are passing and documentations is updated. 
- Never mark any feature / bug complete unless all tests are passing and documentation is updated
- Always write internal developer documentation if needed in the `/project/dev/docs` folder and keep it updated
- If there is a major decision or change that has happened in the repository, add a dated one-line entry to `.memory/timeline/<YYYY-MM>.md`, refresh `.memory/index.md` if the current state changed, and update the `./docs` folder
- Before commit always check if the code build, lint, tests and smoke tests are passing in local to save building time in Cloud
- Whenever you commit some code, always make sure you write proper description of the change
- Always raise PRs against `main`
- No commit should be without a Github Issue. If there is no issue or adhoc work, make sure to create an issue with details and then proceed to write the code.
- Always create branch names and commit messages following conventional commits principles. Don't use model-name-feature-random patterns, keep it simple so that it conveys exactly what is going on here.

## Mandatory Quality Gates (ALL agents must follow)

Every agent — main or subagent — MUST complete these in local before marking work done or committing:

1. **Unit Tests**: Write unit tests for every new feature or bug fix. No exceptions.
2. **E2E Tests**: Write or update E2E tests for every PR.
3. **Lint + Format Check**: Run linter and formatter before commit.
4. **Sanity Check**: Run full test suite + type check to verify nothing is broken.

## Coding Standards

- Follow strict coding standard maximize for explanability to humans
- While designing any solution think in SOLID, DRY, KISS and applicable design patterns
- Always write tests first and then do the development later
- A feature is only complete when all tests are passed and you can provide proof of complete.
- All the code must have comments, which humans can understand easily with the context of this repo.
- Always keep the docs updated incase there are breaking changes highlight early

## Ways of working

1. Find relevant skills in the project which can help achieve the goal better
2. Always use sub-agent driven development for long horizon goals and use skills that are in the system to assist in brainstorming or planning and execution
3. Always use subagents for coding tasks example Sonnet5 for coding while Opus for review and glueing everything and Fable for judgement
4. Use a working file in `.memory` for context handover if its not there create the folder but don't push it in repo.
5. The core engine which powers multiple applications, whenever you propose a solution think about legacy support and the entire work that has already happened?

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

## Writing Standards

- Be concise and direct - remove unneccessary adjective and verbose descriptions
- Use active voice - "Creates agent" not "Agent is created".
- For Documentation write like prescription which brief and concise as AI Agents are going to read it. 
- For PR's answer these 3 sub-question in concise and dot dashes, then go in technical details:
  - what is happening here? 
  - why this was required? 
  - how does it impact the end customer?
