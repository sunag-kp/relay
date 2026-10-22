import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App } from "./App";
import type { ProvidersView, UsageSummary } from "./api";

const totals = {
  requests: 11, errors: 1, cache_hits: 2, semantic_hits: 1, cache_hit_rate: 0.1818,
  prompt_tokens: 38, completion_tokens: 216, total_tokens: 254, cost_usd: 0.000077,
  saved_usd: 0.0000775, avg_latency_ms: 31.5,
};

const summary: UsageSummary = {
  since: "2026-10-16",
  until: "2026-10-22",
  totals,
  by_day: [{ ...totals, day: "2026-10-22" }],
  by_key: [{ ...totals, api_key_id: "k1", name: "search", owner: "team-search" }],
  by_route: [{ ...totals, route: "relay-mock" }],
  by_provider: [{ ...totals, provider: "mock-alpha" }],
};

const providers: ProvidersView = {
  replicas: ["replica-a", "replica-b"],
  providers: [
    {
      provider: "mock-alpha", kind: "openai_compatible", configured: true, worst_state: "open",
      replicas: { "replica-a": "open", "replica-b": "closed" },
      failure_rate: { "replica-a": 0.8, "replica-b": 0 },
    },
  ],
  latency_ms: {},
  routes: [],
};

function fakeFetch(status = 200) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.startsWith("/admin/usage/summary")
      ? summary
      : url.startsWith("/admin/providers")
        ? providers
        : url.startsWith("/info")
          ? { name: "relay", version: "1.0.0", replica: "r", models: ["relay-mock"], demo_api_key: "rk_demo", dashboard_public: true }
          : [];
    if (status !== 200) {
      return new Response(JSON.stringify({ error: { message: "Admin endpoints need 'Authorization: Bearer <ADMIN_TOKEN>'." } }), { status });
    }
    return new Response(JSON.stringify(body), { status: 200 });
  });
}

test("renders KPIs, breakdowns and provider health from the admin API", async () => {
  const fetchImpl = fakeFetch();
  render(<App fetchImpl={fetchImpl as unknown as typeof fetch} />);

  expect(await screen.findByText("Cache hit rate")).toBeInTheDocument();
  expect(screen.getAllByText("18%").length).toBeGreaterThan(0);
  expect(screen.getByRole("region", { name: "By API key" })).toHaveTextContent("search");

  const health = await screen.findByRole("region", { name: "Provider health" });
  // status is carried by a word, not colour alone
  expect(within(health).getAllByText("Tripped").length).toBeGreaterThan(0);
  expect(within(health).getByText("Healthy")).toBeInTheDocument();
  expect(within(health).getByText("80% failures")).toBeInTheDocument();
});

test("each chart has a table view", async () => {
  render(<App fetchImpl={fakeFetch() as unknown as typeof fetch} />);
  const chart = await screen.findByRole("region", { name: "Requests per day" });
  fireEvent.click(within(chart).getByRole("button", { name: "Table" }));
  const table = within(chart).getByRole("table");
  expect(within(table).getByText("10/22")).toBeInTheDocument();
  expect(within(table).getByText("9")).toBeInTheDocument(); // 11 requests - 2 cache hits
});

test("changing the range refetches with the new window", async () => {
  const fetchImpl = fakeFetch();
  render(<App fetchImpl={fetchImpl as unknown as typeof fetch} />);
  await screen.findByText("Cache hit rate");
  fireEvent.click(screen.getByRole("button", { name: "30 days" }));
  await waitFor(() =>
    expect(fetchImpl.mock.calls.some(([url]) => String(url).includes("days=30"))).toBe(true),
  );
});

test("asks for the admin token when the API refuses", async () => {
  render(<App fetchImpl={fakeFetch(401) as unknown as typeof fetch} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Enter the admin token");
});

test("shows a ready-to-run request with the public demo key", async () => {
  render(<App fetchImpl={fakeFetch() as unknown as typeof fetch} />);
  const card = await screen.findByRole("region", { name: "Try it" });
  expect(card).toHaveTextContent("Bearer rk_demo");
  expect(card).toHaveTextContent('"model": "relay-mock"');
});
