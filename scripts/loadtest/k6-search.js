// k6-search.js -- Azure deploy load test (#326, epic #320).
//
// Run by scripts/deploy-azure.sh --loadtest right after the health gate
// passes, against the freshly deployed environment. Not part of any CI
// lane -- it needs a live API_URL + API_KEY, which only exist after a real
// `apply` (see .github/workflows/azure-terraform.yml, which deliberately
// carries no cloud credentials and never runs this).
//
// SLO sources (do not raise without updating both):
//   - p(95) < 2000ms: the same floor services/inh-public-api-svc/tests/
//     benchmark/test_search_latency_throughput.py enforces in CI
//     (P95_LATENCY_SLO_MS). A prod deploy that cannot clear the floor CI
//     already gates on is not ready to serve traffic.
//   - 20 requests/s steady state: the QPS target set by epic #320.
//
// Usage (env vars; scripts/deploy-azure.sh sets these for you):
//   API_URL             https://<api_fqdn>            (required, no trailing slash)
//   API_KEY              ink_...                        (required)
//   API_WORKSPACE_ID     ws_...                          (optional; the deploy
//                         script's bootstrapped key is already workspace-scoped,
//                         so omitting this still fans out to just that workspace --
//                         see docs/access-control.md -- but a caller hitting the
//                         script directly with a user-scoped key should set it)
//
// Run directly:
//   API_URL=https://api.example.com API_KEY=ink_xxx k6 run scripts/loadtest/k6-search.js
import http from "k6/http";
import { check } from "k6";

const API_URL = __ENV.API_URL;
const API_KEY = __ENV.API_KEY;
const API_WORKSPACE_ID = __ENV.API_WORKSPACE_ID || "";

if (!API_URL) {
  throw new Error("API_URL env var is required, e.g. https://api.<env>.example.com");
}
if (!API_KEY) {
  throw new Error("API_KEY env var is required (see scripts/dev/bootstrap.sh / deploy-azure.sh output)");
}

// Short, doc-ish queries representative of real search traffic -- varied
// enough that the target isn't trivially cacheable at a single embedding.
const QUERIES = [
  "how do I reset my password",
  "refund policy for annual plans",
  "onboarding checklist for new hires",
  "api rate limit defaults",
  "data retention and deletion policy",
  "how to invite a teammate to a workspace",
  "billing invoice history export",
  "security compliance certifications",
  "supported document upload formats",
  "how to rotate an api key",
];

export const options = {
  scenarios: {
    // 30s warmup at a light rate: lets connection pools, JIT caches, and
    // autoscaled replicas settle before the steady-state window is measured.
    warmup: {
      executor: "constant-arrival-rate",
      rate: 5,
      timeUnit: "1s",
      duration: "30s",
      preAllocatedVUs: 10,
      maxVUs: 30,
    },
    // The actual target: sustained 20 QPS for 5 minutes (#320).
    steady_state: {
      executor: "constant-arrival-rate",
      rate: 20,
      timeUnit: "1s",
      duration: "5m",
      startTime: "30s",
      preAllocatedVUs: 40,
      maxVUs: 100,
    },
  },
  thresholds: {
    // Same floor CI already enforces (P95_LATENCY_SLO_MS) -- a prod deploy
    // that cannot clear it is not ready to serve traffic.
    http_req_duration: ["p(95)<2000"],
    // >1% failure at steady 20 QPS means the deploy is not production-ready.
    http_req_failed: ["rate<0.01"],
  },
};

export default function search() {
  const query = QUERIES[Math.floor(Math.random() * QUERIES.length)];

  const headers = {
    "Content-Type": "application/json",
    "X-API-Key": API_KEY,
  };
  if (API_WORKSPACE_ID) {
    headers["X-Workspace-Id"] = API_WORKSPACE_ID;
  }

  const res = http.post(
    `${API_URL}/v1/search`,
    JSON.stringify({ query, limit: 5 }),
    { headers, tags: { name: "POST /v1/search" } },
  );

  check(res, {
    "status is 200": (r) => r.status === 200,
    "has results array": (r) => {
      try {
        return Array.isArray(JSON.parse(r.body).results);
      } catch (e) {
        return false;
      }
    },
  });
}
