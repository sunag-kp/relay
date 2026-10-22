import { useState, type ReactNode } from "react";

interface Props {
  title: string;
  legend?: ReactNode;
  chart: ReactNode;
  table: ReactNode;
}

/** A chart with its table-view twin: every value stays reachable without hover or colour. */
export function ChartCard({ title, legend, chart, table }: Props) {
  const [asTable, setAsTable] = useState(false);
  return (
    <section className="card" aria-label={title}>
      <div className="card-head">
        <h2>{title}</h2>
        <button className="btn" type="button" onClick={() => setAsTable(!asTable)} aria-pressed={asTable}>
          {asTable ? "Chart" : "Table"}
        </button>
      </div>
      {!asTable && legend}
      {asTable ? <div className="table-wrap">{table}</div> : chart}
    </section>
  );
}
