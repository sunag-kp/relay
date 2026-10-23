// k6 load test for chat completions at a constant arrival rate (open model: requests keep
// arriving at RATE/s whether or not earlier ones finished, so queueing shows up as latency
// instead of silently lowering the offered load).
//
// TARGET=gateway: through nginx -> gateway replicas -> mock provider.
// TARGET=mock:    straight to the mock provider (the baseline the gateway adds to).
// CACHE=bypass sends unique prompts with Cache-Control: no-cache, no-store (every request goes
// upstream, the cache is skipped); CACHE=cold sends unique prompts with the cache on (exact
// miss, semantic search, upstream, then both caches written: the default path for new
// prompts); CACHE=hit repeats one prompt (served from the exact cache after the first).
import http from "k6/http";
import { check } from "k6";

const RATE = Number(__ENV.RATE || 100);
const TARGET = __ENV.TARGET || "gateway";
const CACHE = __ENV.CACHE || "bypass";

export const options = {
  discardResponseBodies: true,
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
  scenarios: {
    load: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: __ENV.DURATION || "30s",
      preAllocatedVUs: Math.max(20, Math.ceil(RATE / 4)),
      maxVUs: Math.max(100, RATE * 2),
    },
  },
};

const url =
  TARGET === "mock"
    ? `${__ENV.MOCK_URL || "http://localhost:9000"}/p/alpha/v1/chat/completions`
    : `${__ENV.BASE_URL || "http://localhost:8080"}/v1/chat/completions`;

// Cold prompts are new sentences without digits, so they share one semantic-cache namespace
// (and its HNSW search) the way real traffic does; digits would give each its own namespace.
const WORDS = (
  "account api balance batch billing bucket cache capacity cluster config cost customer data " +
  "database deploy error event export feature gateway graph health index invoice key latency " +
  "limit log metric migration model network node order outage partition payment pipeline plan " +
  "policy queue quota region release replica report request retry route schema search secret " +
  "server service session shard storage stream tenant ticket timeout token trace upgrade user"
).split(" ");

function sentence(words) {
  const picked = [];
  for (let i = 0; i < words; i++) picked.push(WORDS[Math.floor(Math.random() * WORDS.length)]);
  return `Explain how the ${picked.join(" ")} works`;
}

function prompt() {
  if (CACHE === "hit") return "What is the capital of France?";
  if (CACHE === "cold") return sentence(8);
  return `load ${__VU}-${__ITER}-${Date.now()}`;
}

export default function () {
  const headers = { "Content-Type": "application/json" };
  if (TARGET === "gateway") headers.Authorization = `Bearer ${__ENV.API_KEY}`;
  if (CACHE === "bypass") headers["Cache-Control"] = "no-cache, no-store";
  const body = JSON.stringify({
    model: TARGET === "mock" ? "mock-large" : "relay-mock",
    messages: [{ role: "user", content: prompt() }],
    max_tokens: 24,
  });
  const res = http.post(url, body, { headers, timeout: "30s" });
  check(res, { "status is 200": (r) => r.status === 200 });
}

export function handleSummary(data) {
  return { [__ENV.OUT || "/out/summary.json"]: JSON.stringify(data) };
}
