import type { BreakerState, ProvidersView } from "../api";
import { formatPercent } from "../format";

const LABEL: Record<BreakerState, string> = { closed: "Healthy", half_open: "Probing", open: "Tripped" };
const ICON: Record<BreakerState, string> = { closed: "✓", half_open: "~", open: "✕" };

/** Status colour always comes with an icon and a word, never colour alone. */
export function StatusBadge({ state }: { state: BreakerState }) {
  return (
    <span className={`status ${state}`}>
      <span className="dot" aria-hidden="true">{ICON[state]}</span>
      {LABEL[state]}
    </span>
  );
}

export function ProviderHealth({ view }: { view: ProvidersView }) {
  return (
    <section className="card" aria-label="Provider health">
      <div className="card-head">
        <h2>Provider health (circuit breakers)</h2>
        <span className="muted">{view.replicas.length} live replica{view.replicas.length === 1 ? "" : "s"}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Provider</th>
              <th>Overall</th>
              {view.replicas.map((replica) => (
                <th key={replica} title={replica}>{replica.slice(0, 12)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.providers.map((p) => (
              <tr key={p.provider}>
                <td>
                  {p.provider}
                  <div className="muted" style={{ fontSize: 12 }}>
                    {p.kind}
                    {!p.configured && " · not configured (no API key)"}
                  </div>
                </td>
                <td><StatusBadge state={p.worst_state} /></td>
                {view.replicas.map((replica) => (
                  <td key={replica}>
                    <StatusBadge state={p.replicas[replica] ?? "closed"} />
                    <div className="muted" style={{ fontSize: 12 }}>
                      {formatPercent(p.failure_rate[replica] ?? 0)} failures
                    </div>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
