"""Live two-principal tenancy isolation E2E (REST + MCP).

Cross-tenant leakage is this product's worst-case failure mode: issue #1 was a
REAL leak, and ADR 0002 makes the workspace the one and only access-control
boundary (`docs/access-control.md`). Every existing test of that boundary is
OFFLINE -- `tests/security/` drives the auth helpers with a mocked database, so
it proves the *decision* logic, never that a real request through the real
middleware stack against real Postgres/Mongo/Weaviate is actually denied. A
scoping bug below the auth layer (a Weaviate query built without a tenant, a
collection name collision, a handler that reads `document_id` before checking
the workspace) is invisible to all of it.

This file is the first proof with TWO REAL PRINCIPALS against a RUNNING stack:
principal A uploads a document containing a sentinel string, and principal B --
a different user owning a different workspace, seeded by the same
`make bootstrap` -- tries to read, search, and delete it over both REST and
MCP. Both principals are fully provisioned, which is the point: pointing A's
key at an invented workspace id would be rejected at the KEY-BINDING check and
would prove nothing about whether one legitimate tenant can reach another's
content.

The statuses asserted here are the DOCUMENTED ones, not whatever the server
happens to return (`docs/reference/rest-api.md#workspace-scoping` and
`docs/access-control.md`):

- Naming another workspace in `X-Workspace-Id` with a workspace-scoped key is
  a **403** -- the caller's own key binding is the thing being refused, and the
  message names the caller's OWN workspace, which leaks nothing.
- Reading, or deleting, a document that lives in another workspace is a
  **404**, deliberately identical to "no such document anywhere". A 403 here
  would be an existence oracle: a scoped key could enumerate ids in a
  workspace it cannot read by watching 403 vs 404. That distinction is
  load-bearing (#138's follow-up closed exactly this oracle on MCP), so these
  tests assert 404 *and* that it is not 403, and would fail if the codes were
  ever "helpfully" swapped.

This test is marked ``compose`` and is deselected by the default pytest run
(see ``addopts`` in pyproject). Run it against a live stack with::

    make dev            # or: make quickstart
    uv run pytest tests/integration/test_compose_tenancy.py -v --no-cov

Configuration (all have local defaults; override via env):
    PUBLIC_API_URL              default http://localhost:18000
    INTEGRATION_API_KEY         default ink_dev_local_key_001   (principal A)
    INTEGRATION_WORKSPACE_ID    default ws_local_001
    INTEGRATION_API_KEY_B       default ink_dev_local_key_002   (principal B)
    INTEGRATION_WORKSPACE_ID_B  default ws_local_002
    INTEGRATION_TIMEOUT         seconds to wait for ingestion (default 180)
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest

# The MCP handshake helper and its payload parser, imported rather than
# re-implemented so the anyio lesson ``mcp_http_session`` encodes lives in ONE
# place: ``streamablehttp_client`` opens a task group, and pytest-asyncio
# finalizes async-generator FIXTURES in a different task than the one that ran
# the test, which anyio refuses ("Attempted to exit cancel scope in a different
# task"). It is therefore a plain ``asynccontextmanager`` entered and exited
# inside the test coroutine body -- see its docstring. It already takes the key
# as a parameter precisely so a tenancy test can open a session as a different
# principal.
from tests.integration.test_compose_mcp import _structured_payload, _text, mcp_http_session

pytestmark = [pytest.mark.compose, pytest.mark.integration, pytest.mark.slow]

API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:18000").rstrip("/")
TIMEOUT = int(os.environ.get("INTEGRATION_TIMEOUT", "180"))

# Principal A -- owns the content.
API_KEY_A = os.environ.get("INTEGRATION_API_KEY", "ink_dev_local_key_001")
WORKSPACE_A = os.environ.get("INTEGRATION_WORKSPACE_ID", "ws_local_001")

# Principal B -- a DIFFERENT user owning a DIFFERENT workspace, seeded by
# ``scripts/dev/bootstrap.sh``'s second ``seed_principal`` call. B holds the
# same permission set as A (read/write/search), so nothing asserted below can
# pass merely because B's key is under-privileged: every denial here has to
# come from the workspace boundary itself.
API_KEY_B = os.environ.get("INTEGRATION_API_KEY_B", "ink_dev_local_key_002")
WORKSPACE_B = os.environ.get("INTEGRATION_WORKSPACE_ID_B", "ws_local_002")

HEADERS_A = {"X-API-Key": API_KEY_A, "X-Workspace-Id": WORKSPACE_A}
HEADERS_B = {"X-API-Key": API_KEY_B, "X-Workspace-Id": WORKSPACE_B}
# B's key while naming A's workspace: the header/binding mismatch case.
HEADERS_B_CLAIMING_A = {"X-API-Key": API_KEY_B, "X-Workspace-Id": WORKSPACE_A}

# UNIQUE PER MODULE RUN, unlike the fixed marker in test_compose_mcp.py. That
# file wants dedup-by-content-hash (#75) so re-runs reuse one document; here a
# stable sentinel would be actively dangerous -- a leak fixed between runs, or
# a stale document from an earlier run of THIS file already sitting in B's
# workspace, could make "B cannot see the sentinel" pass or fail for reasons
# that have nothing to do with the current code. A fresh uuid means the only
# document in the world carrying this string is the one A uploads seconds
# earlier, in A's workspace.
SENTINEL = f"ZZTENANCY{uuid.uuid4().hex.upper()}"
SEED_FILENAME = f"tenancy-isolation-probe-{SENTINEL}.md"
SEED_CONTENT = (
    f"# Tenancy isolation probe {SENTINEL}\n\n"
    f"This document belongs to workspace {WORKSPACE_A} and must never be "
    f"reachable from another workspace by search, read, or delete. "
    f"Sentinel: {SENTINEL}.\n"
)
SEED_QUERY = f"tenancy isolation probe {SENTINEL}"

# B's own content. Its marker is independent of A's sentinel, and it is never
# mentioned in A's document, so neither can match the other by accident.
DECOY_MARKER = f"ZZDECOY{uuid.uuid4().hex.upper()}"
DECOY_FILENAME = f"tenancy-decoy-{DECOY_MARKER}.md"
DECOY_CONTENT = (
    f"# Tenancy decoy {DECOY_MARKER}\n\n"
    f"This document belongs to workspace {WORKSPACE_B}. It exists so that "
    f"workspace has a real vector collection and tenant of its own. "
    f"Marker: {DECOY_MARKER}.\n"
)
DECOY_QUERY = f"tenancy decoy {DECOY_MARKER}"


def _require_stack(client: httpx.Client) -> None:
    """Skip (don't fail) when no healthy stack is reachable."""
    try:
        resp = client.get(f"{API_URL}/health", timeout=5)
    except httpx.HTTPError as exc:
        pytest.skip(f"public API not reachable at {API_URL}: {exc}")
    if resp.status_code != 200:
        pytest.skip(f"public API unhealthy at {API_URL}: HTTP {resp.status_code}")


def _require_principal_b(client: httpx.Client) -> None:
    """FAIL (don't skip) when B's key isn't usable on a healthy stack.

    Deliberately not a skip. A skip would be indistinguishable from "passed"
    in the merge gate, so a stack seeded by a pre-two-principal bootstrap
    would silently retire the only live cross-tenant check this repo has. A
    missing stack is "nothing to test"; a missing principal is "the fixture is
    wrong, go fix it".
    """
    resp = client.get(f"{API_URL}/v1/documents", headers=HEADERS_B)
    assert resp.status_code == 200, (
        f"principal B ({API_KEY_B} / {WORKSPACE_B}) is not usable: "
        f"HTTP {resp.status_code} {resp.text}. Run `make bootstrap` against this "
        f"stack -- scripts/dev/bootstrap.sh seeds both principals."
    )


@pytest.fixture(scope="module")
def client() -> Iterator[httpx.Client]:
    """HTTP client and the skip gate for the whole file.

    Every test takes it -- including the MCP one, whose body never touches it --
    so a missing stack SKIPS instead of failing inside an MCP handshake, the
    pattern test_compose_mcp.py established.
    """
    with httpx.Client(timeout=30) as c:
        _require_stack(c)
        _require_principal_b(c)
        yield c


def _search(client: httpx.Client, headers: dict[str, str], query: str) -> httpx.Response:
    return client.post(
        f"{API_URL}/v1/search",
        headers={**headers, "Content-Type": "application/json"},
        json={"query": query, "limit": 20},
    )


def _upload_and_await(
    client: httpx.Client,
    headers: dict[str, str],
    filename: str,
    content: str,
    query: str,
    owner: str,
) -> str:
    """Upload a document and block until its OWNER can retrieve it by search.

    Waiting for the owner to SEE it is what makes every "the other tenant
    cannot see it" assertion meaningful: without it, an empty result could just
    mean ingestion had not finished, and the whole file would pass vacuously
    against a stack with a total retrieval outage.
    """
    resp = client.post(
        f"{API_URL}/v1/documents",
        headers=headers,
        files={"file": (filename, content.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 201, f"{owner} upload failed: {resp.status_code} {resp.text}"
    document_id = resp.json()["document_id"]
    assert document_id

    deadline = time.monotonic() + TIMEOUT
    last: dict = {}
    while time.monotonic() < deadline:
        search = _search(client, headers, query)
        assert (
            search.status_code == 200
        ), f"{owner} search failed: {search.status_code} {search.text}"
        last = search.json()
        if document_id in {r["document_id"] for r in last["results"]}:
            return document_id
        time.sleep(3)
    pytest.fail(
        f"{owner}'s document {document_id} did not become searchable to its OWNER within "
        f"{TIMEOUT}s (last total_results={last.get('total_results')}) -- cannot assert "
        f"isolation against content that is not retrievable at all"
    )


def _cleanup(client: httpx.Client, headers: dict[str, str], document_id: str) -> None:
    """Delete a probe document, asserting the owner's DELETE actually worked.

    Asserted, not best-effort. Both probe documents carry a per-run uuid, so
    without a working delete the workspaces grow by two documents on every run.
    It also gives the DELETE verb the owner-side positive control that
    ``test_cross_workspace_delete_blocked`` otherwise lacks: that test asserts
    B's delete is refused with a 404, which proves nothing if DELETE happens to
    be broken for everybody. Here the SAME verb, on the SAME document, by its
    owner, must return 204.
    """
    resp = client.delete(f"{API_URL}/v1/documents/{document_id}", headers=headers)
    assert resp.status_code == 204, (
        f"owner DELETE of probe document {document_id} returned {resp.status_code}, expected 204 "
        f"-- the cross-tenant 404 asserted elsewhere means nothing if DELETE is broken for "
        f"everyone: {resp.text}"
    )


@pytest.fixture(scope="module")
def decoy_document(client: httpx.Client) -> Iterator[str]:
    """A document of B's OWN, uploaded before anything is asserted about B.

    Without this, B's workspace has never been written to, so it has no
    Weaviate collection at all -- and every "B finds nothing" result comes from
    the missing-collection short-circuit in ``search.py`` (``_is_missing_
    collection`` -> empty), not from scoped retrieval. The smoke-lane test would
    then be green on a build where tenant scoping had been removed entirely.
    Seeding B first forces B's collection and per-user tenant into existence, so
    the search that must not see A's content is a REAL query against a REAL
    collection. It doubles as B's owner-side positive control.
    """
    document_id = _upload_and_await(
        client, HEADERS_B, DECOY_FILENAME, DECOY_CONTENT, DECOY_QUERY, owner="principal B"
    )
    yield document_id
    _cleanup(client, HEADERS_B, document_id)


@pytest.fixture(scope="module")
def seeded_document(client: httpx.Client, decoy_document: str) -> Iterator[str]:
    """A's sentinel document -- the content B must never reach.

    Depends on ``decoy_document`` so B's workspace is always populated first;
    ordering matters only in that every B-side assertion in this file then runs
    against a workspace that really exists in the vector store.
    """
    document_id = _upload_and_await(
        client, HEADERS_A, SEED_FILENAME, SEED_CONTENT, SEED_QUERY, owner="principal A"
    )
    yield document_id
    _cleanup(client, HEADERS_A, document_id)


def _sentinel_hits(payload: dict, document_id: str) -> list[dict]:
    """Every result that is, or quotes, A's document."""
    return [
        r
        for r in payload.get("results", [])
        if r.get("document_id") == document_id or SENTINEL in (r.get("content") or "")
    ]


# Smoke: the single cheapest question whose wrong answer is a company-ending
# bug -- "can tenant B search up tenant A's content?". Tagged for the
# every-PR lane (`-m "smoke and compose"`) because a regression here is not
# something to discover on the nightly compose run.
@pytest.mark.smoke
def test_cross_workspace_search_is_empty(
    client: httpx.Client, seeded_document: str, decoy_document: str
) -> None:
    """B searching its OWN workspace never surfaces A's document.

    ``decoy_document`` is not decoration: it puts real content of B's own in
    B's workspace, so B's collection and tenant exist and this search is
    genuinely scoped retrieval rather than the missing-collection
    short-circuit. The first assertion below confirms B's retrieval is alive
    right now -- if B could not find its OWN document, "B found nothing of A's"
    would mean nothing.

    Then the mismatch case: B's key naming A's workspace in the header. Per
    docs/reference/rest-api.md#workspace-scoping that is a 403 -- unlike a
    document read, which is a 404. The asymmetry is intentional and worth
    pinning: the 403 refuses the CALLER'S OWN key binding (and the message
    names B's own workspace, revealing nothing about A), while a 404 on a
    document hides whether the id exists at all.
    """
    own = _search(client, HEADERS_B, DECOY_QUERY)
    assert own.status_code == 200, f"B's decoy search failed: {own.status_code} {own.text}"
    assert decoy_document in {r["document_id"] for r in own.json()["results"]}, (
        f"principal B cannot retrieve its OWN document {decoy_document} -- B's retrieval path "
        f"is not working, so the isolation assertions below would pass vacuously: {own.text}"
    )

    resp = _search(client, HEADERS_B, SEED_QUERY)
    assert (
        resp.status_code == 200
    ), f"B's own-workspace search failed: {resp.status_code} {resp.text}"
    payload = resp.json()

    leaked = _sentinel_hits(payload, seeded_document)
    assert not leaked, (
        f"CROSS-TENANT LEAK: principal B ({WORKSPACE_B}) retrieved workspace {WORKSPACE_A}'s "
        f"document {seeded_document} / sentinel {SENTINEL}: {leaked}"
    )
    # Not asserted here: which workspaces were searched. REST's SearchResponse
    # carries no ``workspaces_searched`` field (MCP's structured payload does),
    # so a `.get()` on it would be a permanently-true assertion. The fan-out
    # scope is pinned in the MCP test below, where the field is real.

    mismatched = _search(client, HEADERS_B_CLAIMING_A, SEED_QUERY)
    assert mismatched.status_code == 403, (
        f"B's key naming workspace {WORKSPACE_A} returned {mismatched.status_code}, expected the "
        f"documented 403: {mismatched.text}"
    )
    # Whatever the body says, it must not carry A's content back.
    assert SENTINEL not in mismatched.text


def test_cross_workspace_document_read_blocked(client: httpx.Client, seeded_document: str) -> None:
    """Direct reads of A's document id by B are 404, never 403.

    Knowing the id is the strongest position an attacker can be in short of
    holding a valid key for the workspace, so this is the case that matters:
    both the document metadata route and the chunk route must answer exactly
    as they would for an id that never existed.
    """
    for path in (
        f"/v1/documents/{seeded_document}",
        f"/v1/chunks/{seeded_document}",
        f"/v1/chunks/{seeded_document}/context",
    ):
        resp = client.get(f"{API_URL}{path}", headers=HEADERS_B)
        assert resp.status_code == 404, (
            f"GET {path} as principal B returned {resp.status_code}, expected 404 "
            f"(403 would confirm the id exists -- an existence oracle across workspaces): "
            f"{resp.text}"
        )
        assert SENTINEL not in resp.text, f"GET {path} leaked A's content to B: {resp.text}"

        # Negative control, per route: the SAME url IS readable by its owner.
        # Without it a route that 404s for everyone -- a typo in the path, a
        # route removed, an id that never landed -- would read as isolation.
        owner = client.get(f"{API_URL}{path}", headers=HEADERS_A)
        assert owner.status_code == 200, (
            f"GET {path} as the OWNER returned {owner.status_code} -- B's 404 above proves "
            f"nothing if nobody can read this route: {owner.text}"
        )
        assert SENTINEL in owner.text, f"GET {path} as the owner returned no sentinel content"


def test_cross_workspace_delete_blocked(client: httpx.Client, seeded_document: str) -> None:
    """B cannot delete A's document, and the document survives the attempt.

    The 404 alone is not enough. A handler that deleted first and reported the
    workspace mismatch afterwards, or that removed the vectors before checking,
    would also answer 404 -- so this re-reads the document AND its chunks as A
    afterwards. Destroying data you cannot read is a leak in the other
    direction.
    """
    before_resp = client.get(f"{API_URL}/v1/documents/{seeded_document}", headers=HEADERS_A)
    assert before_resp.status_code == 200, (
        f"pre-read of A's own document failed: {before_resp.status_code} {before_resp.text} "
        f"-- nothing below can be interpreted without a known-good starting state"
    )
    before = before_resp.json()

    resp = client.delete(f"{API_URL}/v1/documents/{seeded_document}", headers=HEADERS_B)
    assert (
        resp.status_code == 404
    ), f"DELETE as principal B returned {resp.status_code}, expected 404: {resp.text}"

    after_resp = client.get(f"{API_URL}/v1/documents/{seeded_document}", headers=HEADERS_A)
    assert after_resp.status_code == 200, (
        f"A's document is gone after B's delete attempt: {after_resp.status_code} "
        f"{after_resp.text}"
    )
    after = after_resp.json()
    assert after["status"] == before["status"]
    assert after["chunk_count"] == before["chunk_count"]

    chunks = client.get(f"{API_URL}/v1/chunks/{seeded_document}", headers=HEADERS_A)
    assert chunks.status_code == 200, f"A's chunks are gone: {chunks.status_code} {chunks.text}"
    assert SENTINEL in chunks.text, "A's indexed content was damaged by B's delete attempt"


async def test_mcp_cross_workspace_search_is_empty(
    client: httpx.Client, seeded_document: str, decoy_document: str
) -> None:
    """The same boundary over MCP, the transport an agent actually uses.

    Worth its own test rather than trusting the REST result: on MCP the
    workspace is a tool ARGUMENT, not a header, and the tool handlers resolve
    it through their own call sites. The two surfaces share
    ``get_authorized_workspace_ids`` (#138) -- this asserts that sharing still
    holds end to end, over the wire, rather than by reading the code.

    ``decoy_document`` is requested explicitly (``seeded_document`` already
    pulls it in) to make the dependency visible: B's search here has to run
    against a workspace that really exists in the vector store, not one whose
    collection was never created.
    """
    async with mcp_http_session(API_KEY_B) as session:
        own = await session.call_tool(
            "search_documents",
            {"query": SEED_QUERY, "workspace_id": WORKSPACE_B, "limit": 20},
        )
        claiming_a = await session.call_tool(
            "search_documents",
            {"query": SEED_QUERY, "workspace_id": WORKSPACE_A, "limit": 20},
        )

    # NEGATIVE CONTROL, and the reason this test is not vacuous: the SAME query
    # over the SAME transport, as A, must FIND the document. Without it, an MCP
    # search that returned nothing to anybody -- a broken tool schema, a
    # transport regression, an unreachable Weaviate -- would read as perfect
    # isolation. The REST tests get this for free from the seeding fixture,
    # which polls A's search until the document appears; MCP has to ask.
    async with mcp_http_session(API_KEY_A) as owner_session:
        owner = await owner_session.call_tool(
            "search_documents",
            {"query": SEED_QUERY, "workspace_id": WORKSPACE_A, "limit": 20},
        )
    assert owner.isError is False, f"A's own MCP search failed: {owner}"
    assert _sentinel_hits(_structured_payload(owner), seeded_document), (
        f"A cannot retrieve its OWN document {seeded_document} over MCP -- the isolation "
        f"assertions below would pass vacuously: {_text(owner)[:2000]}"
    )

    assert own.isError is False, f"B's own-workspace MCP search failed: {own}"
    own_payload = _structured_payload(own)

    # Assert on the RESULTS, not on the rendered text. The tool echoes the
    # query back to the agent ("No results found for: <query> in workspace
    # ..."), and the query contains the sentinel by construction -- a naive
    # ``SENTINEL not in text`` reports every empty result as a leak. (It did:
    # that was this test's first live run.) The sentinel is only evidence of a
    # leak when it comes back as retrieved CONTENT.
    leaked = _sentinel_hits(own_payload, seeded_document)
    assert not leaked, (
        f"CROSS-TENANT LEAK over MCP: principal B searching {WORKSPACE_B} retrieved "
        f"workspace {WORKSPACE_A}'s document {seeded_document} / sentinel {SENTINEL}: {leaked}"
    )
    # The document id, unlike the sentinel, is never echoed by the tool, so it
    # is safe to assert against the whole rendered text -- this catches a leak
    # into the prose an agent reads even if the structured block were clean.
    assert seeded_document not in _text(own), (
        f"CROSS-TENANT LEAK over MCP: B's search cited A's document {seeded_document}: "
        f"{_text(own)[:2000]}"
    )
    # The fan-out scope itself, which REST does not report: B's search must
    # have touched B's workspace and nothing else. A search that reached A's
    # collection and merely ranked nothing above threshold is still a leak --
    # one waiting for a better-matching query.
    assert own_payload["workspaces_searched"] == [WORKSPACE_B], (
        f"B's MCP search fanned out beyond its own workspace: "
        f"{own_payload['workspaces_searched']}"
    )

    # Naming A's workspace as the tool argument is the MCP analogue of the
    # rejected header, and must fail rather than quietly fall back to B's
    # authorized set (a silent fallback would hide a scoping bug behind an
    # empty-looking success).
    assert (
        claiming_a.isError is True
    ), f"B's MCP search named workspace {WORKSPACE_A} and was NOT rejected: {claiming_a}"
    assert seeded_document not in _text(claiming_a)
