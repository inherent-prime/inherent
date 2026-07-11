# CLAUDE.md

Guidance for working in this repository.

## General Guidance
- Consult the knowledge-graph `graphify-out/` when require context about the repo. If its not there, ask the user to build one.
- Always thinks the end user as an AI agent, so always develop solutions that is performant and cost effective for the end user.
- The Definition of Done is considered when all tests are passing and documentations is updated. 

## Coding Standards

- Follow strict coding standard maximize for explanability to humans
- While designing any solution think in SOLID, DRY, KISS and whatever applicable. 
- Always write tests first and then do the development later
- A feature is only complete when all tests are passed and you can provide proof of complete.
- All the code must have comments, which humans can understand easily with the context of this repo.
- Always keep the docs updated incase there are breaking changes highlight early
- Incase of long tasks always use sub-agents to achieve the goal.

## Defect Prevention

Rules from the #98/#99/#100 retrospective. Apply before closing any task:

- **Pattern sweep**: after fixing a bug, grep both services for the same defect
  pattern. State the sweep result (hits or "clean") in the PR description.
- **Dual-surface failure parity**: when touching a capability that exists on
  both REST and MCP (upload, refresh, delete, ...), diff the two handlers'
  failure paths — MQ down, DB down, vector store down, not-found, permission.
  Both surfaces must leave the same document state and surface an error. Pin
  any pair you touch in
  `services/inh-public-api-svc/tests/contract/test_failure_parity.py`.
- **Compensate state mutations**: a state write followed by a publish (or any
  second fallible step) needs a tested compensating mark-failed path.
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

## Writing Standards

- Be concise and direct - remove unneccessary adjective and verbose descriptions
- Use active voice - "Creates agent" not "Agent is created".
- For Documentation write like prescription which brief and concise as AI Agents are going to read it. 
 


