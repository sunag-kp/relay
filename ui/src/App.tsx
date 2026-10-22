import { useMemo, useState } from "react";
import { RelayApi } from "./api";
import { Alerts, RecentRequests } from "./components/Activity";
import { Breakdown } from "./components/Breakdown";
import { RequestsChart, SpendChart } from "./components/Charts";
import { KeysPanel } from "./components/KeysPanel";
import { Kpis } from "./components/Kpis";
import { ProviderHealth } from "./components/ProviderHealth";
import { TryIt } from "./components/TryIt";
import { usePolling } from "./usePolling";

const RANGES = [
  { days: 1, label: "Today" },
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
];
const TOKEN_KEY = "relay-admin-token";

function storedToken(): string {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function App({ fetchImpl }: { fetchImpl?: typeof fetch }) {
  const [token, setToken] = useState(storedToken);
  const [draft, setDraft] = useState(token);
  const [days, setDays] = useState(7);
  const [keyId, setKeyId] = useState("");
  const api = useMemo(() => new RelayApi(token, fetchImpl), [token, fetchImpl]);

  const summary = usePolling(() => api.summary(days, keyId || undefined), 15_000, [api, days, keyId]);
  const providers = usePolling(() => api.providers(), 5_000, [api]);
  const events = usePolling(() => api.events(15), 15_000, [api]);
  const alerts = usePolling(() => api.alerts(), 60_000, [api]);
  const info = usePolling(() => api.info(), 300_000, [api]);
  const keys = usePolling(() => (token ? api.keys() : Promise.resolve([])), 60_000, [api, token]);

  function saveToken() {
    try {
      window.sessionStorage.setItem(TOKEN_KEY, draft);
    } catch {
      /* storage unavailable: keep it in memory only */
    }
    setToken(draft);
  }

  const needsToken = summary.error !== null && !summary.data;
  const s = summary.data;

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand">
          <h1>Relay</h1>
          <span className="muted">LLM gateway · usage, cost, cache and provider health</span>
        </div>
        <form
          className="form-row"
          onSubmit={(e) => {
            e.preventDefault();
            saveToken();
          }}
        >
          <label className="sr-only" htmlFor="token">Admin token</label>
          <input
            id="token"
            type="password"
            placeholder="Admin token (for key management)"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            autoComplete="off"
          />
          <button className="btn" type="submit">{token ? "Update" : "Unlock"}</button>
        </form>
      </header>

      {needsToken && (
        <div className="banner error" role="alert">
          {summary.error}
          {summary.error?.includes("ADMIN_TOKEN") || summary.error?.startsWith("401")
            ? " Enter the admin token above to load the dashboard."
            : ""}
        </div>
      )}

      <div className="filters" role="group" aria-label="Filters">
        <div className="segmented" role="group" aria-label="Time range">
          {RANGES.map((range) => (
            <button
              key={range.days}
              type="button"
              aria-pressed={days === range.days}
              onClick={() => setDays(range.days)}
            >
              {range.label}
            </button>
          ))}
        </div>
        <label className="sr-only" htmlFor="key-filter">API key</label>
        <select id="key-filter" value={keyId} onChange={(e) => setKeyId(e.target.value)}>
          <option value="">All API keys</option>
          {(s?.by_key ?? []).map((k) => (
            <option key={k.api_key_id} value={k.api_key_id}>{k.name} ({k.owner})</option>
          ))}
        </select>
        {s && <span className="muted">{s.since} to {s.until} (UTC)</span>}
      </div>

      {s && (
        <div className={summary.loading ? "stale" : undefined}>
          <Kpis totals={s.totals} />
          <div className="grid two">
            <RequestsChart days={s.by_day} />
            <SpendChart days={s.by_day} />
          </div>
          <div className="grid two">
            <Breakdown
              title="By API key"
              empty="No traffic in this range."
              rows={s.by_key.map((k) => ({ ...k, label: k.name, detail: k.owner }))}
            />
            <Breakdown
              title="By model (route)"
              empty="No traffic in this range."
              rows={s.by_route.map((r) => ({ ...r, label: r.route }))}
            />
          </div>
        </div>
      )}

      <div className="grid" style={{ marginBottom: 16 }}>
        {info.data && <TryIt info={info.data} />}
        {providers.data && <ProviderHealth view={providers.data} />}
        {events.data && <RecentRequests events={events.data} />}
        {alerts.data && <Alerts alerts={alerts.data} />}
        {token && keys.data && <KeysPanel api={api} keys={keys.data} onChange={keys.reload} />}
        {token && keys.error && (
          <div className="banner error" role="alert">
            Key management: {keys.error}
          </div>
        )}
      </div>
    </main>
  );
}
