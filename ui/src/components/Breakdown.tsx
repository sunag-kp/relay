import type { Totals } from "../api";
import { formatCount, formatMs, formatPercent, formatUsd } from "../format";

interface Row extends Totals {
  label: string;
  detail?: string;
}

/** One table per breakdown; under each spend figure, a thin single-colour bar for scale. */
export function Breakdown({ title, rows, empty }: { title: string; rows: Row[]; empty: string }) {
  const maxCost = Math.max(0, ...rows.map((r) => r.cost_usd));
  return (
    <section className="card" aria-label={title}>
      <div className="card-head"><h2>{title}</h2></div>
      {rows.length === 0 ? (
        <p className="muted">{empty}</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th className="num">Requests</th>
                <th className="num">Tokens</th>
                <th className="num">Cache hits</th>
                <th className="num">Avg latency</th>
                <th className="num">Spend</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.label}>
                  <td>
                    {row.label}
                    {row.detail && <div className="muted" style={{ fontSize: 12 }}>{row.detail}</div>}
                  </td>
                  <td className="num">{formatCount(row.requests)}</td>
                  <td className="num">{formatCount(row.total_tokens)}</td>
                  <td className="num">{formatPercent(row.cache_hit_rate)}</td>
                  <td className="num">{formatMs(row.avg_latency_ms)}</td>
                  <td className="num">
                    {formatUsd(row.cost_usd)}
                    <div
                      aria-hidden="true"
                      style={{
                        height: 4,
                        marginTop: 3,
                        marginLeft: "auto",
                        width: `${maxCost ? Math.max(2, (row.cost_usd / maxCost) * 64) : 0}px`,
                        background: "var(--series-1)",
                        borderRadius: 2,
                      }}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
