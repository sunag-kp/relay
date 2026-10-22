import type { Totals } from "../api";
import { formatCount, formatMs, formatPercent, formatUsd } from "../format";

export function Kpis({ totals }: { totals: Totals }) {
  const errorRate = totals.requests ? totals.errors / totals.requests : 0;
  const tiles = [
    { label: "Requests", value: formatCount(totals.requests), sub: `${formatCount(totals.total_tokens)} tokens` },
    {
      label: "Cache hit rate",
      value: formatPercent(totals.cache_hit_rate),
      sub: `${formatCount(totals.cache_hits)} hits, ${formatCount(totals.semantic_hits)} semantic`,
    },
    { label: "Provider spend", value: formatUsd(totals.cost_usd), sub: "estimated from token prices" },
    { label: "Saved by cache", value: formatUsd(totals.saved_usd), sub: "spend avoided by cache hits" },
    { label: "Error rate", value: formatPercent(errorRate), sub: `${formatCount(totals.errors)} failed` },
    { label: "Avg latency", value: formatMs(totals.avg_latency_ms), sub: "end to end, incl. cache hits" },
  ];
  return (
    <div className="grid kpis">
      {tiles.map((tile) => (
        <div className="card tile" key={tile.label}>
          <div className="label">{tile.label}</div>
          <div className="value">{tile.value}</div>
          <div className="sub">{tile.sub}</div>
        </div>
      ))}
    </div>
  );
}
