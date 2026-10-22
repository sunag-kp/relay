import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
} from "recharts";
import type { NameType, ValueType } from "recharts/types/component/DefaultTooltipContent";
import type { UsageSummary } from "../api";
import { formatCount, formatUsd, shortDay } from "../format";
import { ChartCard } from "./ChartCard";

const AXIS = { stroke: "var(--axis)", tick: { fill: "var(--text-muted)", fontSize: 12 } };
// 2px surface-coloured stroke = the 2px gap between adjacent and stacked fills.
const GAP = { stroke: "var(--surface-1)", strokeWidth: 2 };

type Day = UsageSummary["by_day"][number];

function rows(days: Day[]) {
  return days.map((d) => ({
    day: shortDay(d.day),
    provider: d.requests - d.cache_hits,
    cached: d.cache_hits,
    cost: d.cost_usd,
    saved: d.saved_usd,
  }));
}

function TooltipBox({ active, payload, label }: TooltipContentProps<ValueType, NameType>) {
  if (!active || !payload?.length) return null;
  return (
    <div className="tooltip">
      <div className="secondary">{label}</div>
      {payload.map((item) => (
        <div className="row" key={String(item.dataKey)}>
          <span>
            <span className="swatch" style={{ background: item.color }} />
            {item.name}
          </span>
          <strong>{item.dataKey === "cost" ? formatUsd(Number(item.value)) : formatCount(Number(item.value))}</strong>
        </div>
      ))}
    </div>
  );
}

export function RequestsChart({ days }: { days: Day[] }) {
  const data = rows(days);
  return (
    <ChartCard
      title="Requests per day"
      legend={
        <div className="legend" aria-hidden="true">
          <span><span className="swatch" style={{ background: "var(--series-1)" }} />Served by a provider</span>
          <span><span className="swatch" style={{ background: "var(--series-2)" }} />Served from cache</span>
        </div>
      }
      chart={
        <div style={{ height: 240 }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 12, right: 8, bottom: 0, left: -8 }}>
              <CartesianGrid vertical={false} stroke="var(--grid)" />
              <XAxis dataKey="day" {...AXIS} tickLine={false} />
              <YAxis {...AXIS} tickLine={false} axisLine={false} allowDecimals={false} tickFormatter={formatCount} />
              <Tooltip content={TooltipBox} cursor={{ fill: "var(--surface-2)" }} />
              <Bar dataKey="provider" name="Served by a provider" stackId="r" fill="var(--series-1)" {...GAP} />
              <Bar dataKey="cached" name="Served from cache" stackId="r" fill="var(--series-2)" radius={[4, 4, 0, 0]} {...GAP} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      }
      table={
        <table>
          <thead>
            <tr><th>Day</th><th className="num">Provider</th><th className="num">Cache</th><th className="num">Total</th></tr>
          </thead>
          <tbody>
            {data.map((r) => (
              <tr key={r.day}>
                <td>{r.day}</td>
                <td className="num">{formatCount(r.provider)}</td>
                <td className="num">{formatCount(r.cached)}</td>
                <td className="num">{formatCount(r.provider + r.cached)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      }
    />
  );
}

export function SpendChart({ days }: { days: Day[] }) {
  const data = rows(days);
  return (
    <ChartCard
      title="Provider spend per day (USD)"
      chart={
        <div style={{ height: 240 }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 12, right: 8, bottom: 0, left: -4 }}>
              <CartesianGrid vertical={false} stroke="var(--grid)" />
              <XAxis dataKey="day" {...AXIS} tickLine={false} />
              <YAxis {...AXIS} tickLine={false} axisLine={false} tickFormatter={(v: number) => formatUsd(v)} />
              <Tooltip content={TooltipBox} cursor={{ fill: "var(--surface-2)" }} />
              <Bar dataKey="cost" name="Spend" fill="var(--series-1)" radius={[4, 4, 0, 0]} {...GAP} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      }
      table={
        <table>
          <thead>
            <tr><th>Day</th><th className="num">Spend</th><th className="num">Saved by cache</th></tr>
          </thead>
          <tbody>
            {data.map((r) => (
              <tr key={r.day}>
                <td>{r.day}</td>
                <td className="num">{formatUsd(r.cost)}</td>
                <td className="num">{formatUsd(r.saved)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      }
    />
  );
}
