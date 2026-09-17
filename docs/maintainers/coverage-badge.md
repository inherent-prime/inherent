---
search:
  exclude: true
---

# Coverage badge setup

One-time setup for the dynamic coverage badge published by the
`coverage-badge` job in [`.github/workflows/ci.yml`](https://github.com/inherent-prime/inherent/blob/main/.github/workflows/ci.yml).
Tracking issue: #361. Until this is done, the job no-ops (its `if:` guard
checks `vars.COVERAGE_GIST_ID != ''`) and README keeps the static
`coverage-floors...enforced` badge.

## Setup

1. Create a public GitHub Gist (gists must be public for
   `img.shields.io/endpoint` to read them) with one file named
   `inherent-coverage.json`. Seed it with a placeholder so the gist exists:
   ```json
   {"schemaVersion": 1, "label": "coverage", "message": "pending", "color": "lightgrey"}
   ```
   Note the gist ID from its URL (`https://gist.github.com/<owner>/<GIST_ID>`).
2. Create a personal access token scoped to `gist` only (classic PAT with
   the `gist` scope, or a fine-grained PAT with Gists: read and write) on an
   account that owns the gist above.
3. In the repository settings, add:
   - Secret `GIST_SECRET` — the PAT from step 2.
   - Variable `COVERAGE_GIST_ID` — the gist ID from step 1.
4. Replace the static coverage badge in `README.md` with the dynamic
   endpoint (swap the `<img src=...>` on the line commented
   `Coverage badge: swap for the dynamic gist endpoint...`):
   ```
   https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/<gist-owner>/<gist-id>/raw/inherent-coverage.json
   ```
5. Push to `main` (or wait for the next push). The `coverage-badge` job runs,
   writes `inherent-coverage.json` into the gist, and the badge goes live on
   its next fetch.

## How the number is computed

`coverage-badge` runs after `service-checks` and downloads the
`coverage-<service>` artifacts each matrix entry uploads (main-branch push
only). Each artifact is a `coverage json` export of that service's
`.coverage` file. The job sums `totals.covered_lines` and
`totals.num_statements` across all four services and computes:

```
percent = 100 * sum(covered_lines) / sum(num_statements)
```

rounded to one decimal — a weighted average across services, not an average
of each service's percentage, so a large service's coverage counts
proportionally more than a small one's. Color follows the number: `>=90`
brightgreen, `>=80` green, `>=70` yellowgreen, `>=60` yellow, else orange.

## Relation to the enforced floors

The badge is a snapshot, not a gate. The actual enforcement is the
per-service `cov_fail_under` floors in `ci.yml`'s `service-checks` matrix
(currently 82 / 84 / 89 / 98 for inh-ingestion-svc / inh-public-api-svc /
inh-cli / inh-contracts) plus the per-core-module floors below them — those
run on every PR and every push, and only ever ratchet up. The badge number
can therefore sit above the floors (it reflects `main`'s actual coverage) and
will move independently of them; it exists to show trust-signal proof of the
floors' effect, not to replace them. If the gist or `GIST_SECRET` ever go
stale, the badge goes stale too, but no PR or merge is blocked by it — see
the comment above the `coverage-badge` job in `ci.yml` for why it is
deliberately not a required check.
