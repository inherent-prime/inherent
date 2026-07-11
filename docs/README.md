# Inherent Docs

Start here when you need to understand, run, test, or maintain this repository.
This page is organized for agent-first discovery: choose the job you are doing,
then open the smallest relevant document.

## Fast Routes

| If you need to... | Open this |
| --- | --- |
| Start the system locally and run the first upload/search flow | [getting-started/local.md](getting-started/local.md) |
| Harden the demo stack before exposing it to real users/data | [deploy/production.md](deploy/production.md) |
| Copy request examples for every public endpoint | [examples/README.md](examples/README.md) |
| Use sample files for upload testing | [examples/sample-documents/](examples/sample-documents/) |
| Check what belongs in the OSS repository | [maintainers/repository-boundaries.md](maintainers/repository-boundaries.md) |
| Prepare or review a release | [maintainers/releasing.md](maintainers/releasing.md) |

## Agent Reading Order

For most coding or documentation tasks, read in this order:

1. [../README.md](../README.md) for the product and repository overview.
2. This file for docs routing.
3. [getting-started/local.md](getting-started/local.md) if the task involves
   running the system.
4. [examples/README.md](examples/README.md) if the task involves API behavior,
   curl examples, request shape, or response shape.
5. Service READMEs when the task is service-specific:
   [../services/inh-ingestion-svc/Readme.md](../services/inh-ingestion-svc/Readme.md)
   or
   [../services/inh-public-api-svc/Readme.md](../services/inh-public-api-svc/Readme.md).

## Folder Map

```text
docs/
  README.md                    agent-first discovery hub
  getting-started/
    local.md                   local start, upload, ingest, and search guide
  deploy/
    production.md              hardening checklist for real deployments
  examples/
    README.md                  endpoint-by-endpoint curl examples
    sample-documents/          files used by local upload examples
  imgs/
    Hero.png                   README image asset
  maintainers/
    releasing.md               release checklist
    repository-boundaries.md   OSS scope guardrails
```

## Documentation Rules

- Keep the root README light. Put detailed workflows in `docs/`.
- Prefer task-oriented docs over broad reference pages.
- Make docs runnable from the repository root unless the page says otherwise.
- Keep examples aligned with the current Makefile and service behavior.
- Do not document hosted, private, or future behavior that is not present in
  this repository.
