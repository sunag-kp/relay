import type { BudgetAlert, UsageEvent } from "../api";
import { formatMs, formatUsd } from "../format";

export function RecentRequests({ events }: { events: UsageEvent[] }) {
  return (
    <section className="card" aria-label="Recent requests">
      <div className="card-head"><h2>Recent requests</h2></div>
      {events.length === 0 ? (
        <p className="muted">No requests yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Time (UTC)</th><th>Route</th><th>Served by</th><th>Status</th>
                <th className="num">Tokens</th><th className="num">Cost</th><th className="num">Latency</th><th>Replica</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.request_id}>
                  <td>{e.created_at.slice(11, 19)}</td>
                  <td>{e.route}</td>
                  <td>{e.cache === "exact" || e.cache === "semantic" ? `cache (${e.cache})` : (e.provider ?? "-")}</td>
                  <td>{e.status}</td>
                  <td className="num">{e.prompt_tokens + e.completion_tokens}</td>
                  <td className="num">{formatUsd(Number(e.cost_usd))}</td>
                  <td className="num">{formatMs(e.latency_ms)}</td>
                  <td className="muted">{e.replica.slice(0, 12)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function Alerts({ alerts }: { alerts: BudgetAlert[] }) {
  return (
    <section className="card" aria-label="Budget alerts">
      <div className="card-head"><h2>Budget alerts</h2></div>
      {alerts.length === 0 ? (
        <p className="muted">No key has crossed 50% of its monthly budget.</p>
      ) : (
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          {alerts.map((a) => (
            <li key={`${a.api_key_id}-${a.period}-${a.threshold_pct}`}>
              <strong>{a.key_name}</strong> ({a.owner}) reached {a.threshold_pct}% of ${a.budget_usd} in {a.period}
              <span className="muted"> · spent ${Number(a.spend_usd).toFixed(2)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
