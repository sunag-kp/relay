import { useState, type FormEvent } from "react";
import type { ApiKey, CreatedKey, RelayApi } from "../api";
import { formatCount } from "../format";

interface Props {
  api: RelayApi;
  keys: ApiKey[];
  onChange: () => void;
}

export function KeysPanel({ api, keys, onChange }: Props) {
  const [name, setName] = useState("");
  const [owner, setOwner] = useState("default");
  const [rpm, setRpm] = useState(60);
  const [tpd, setTpd] = useState(1_000_000);
  const [created, setCreated] = useState<CreatedKey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setCreated(await api.createKey({ name, owner, rpm_limit: rpm, tpd_limit: tpd }));
      setName("");
      onChange();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(key: ApiKey) {
    if (!window.confirm(`Revoke "${key.name}"? Clients using it start getting 401 at once.`)) return;
    try {
      await api.revokeKey(key.id);
      onChange();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section className="card" aria-label="API keys">
      <div className="card-head"><h2>API keys</h2></div>
      <form className="form-row" onSubmit={create} style={{ marginBottom: 12 }}>
        <label>Name<input type="text" value={name} onChange={(e) => setName(e.target.value)} required /></label>
        <label>Owner<input type="text" value={owner} onChange={(e) => setOwner(e.target.value)} required /></label>
        <label>Requests / min<input type="number" min={1} value={rpm} onChange={(e) => setRpm(Number(e.target.value))} /></label>
        <label>Tokens / day<input type="number" min={1} value={tpd} onChange={(e) => setTpd(Number(e.target.value))} /></label>
        <button className="btn primary" type="submit" disabled={busy || !name}>Create key</button>
      </form>
      {error && <div className="banner error" role="alert">{error}</div>}
      {created && (
        <div className="banner" role="status">
          Key <strong>{created.name}</strong> created. Copy it now; it is not shown again:
          <div className="secret" style={{ marginTop: 6 }}>{created.api_key}</div>
        </div>
      )}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th><th>Owner</th><th>Prefix</th><th className="num">Req/min</th>
              <th className="num">Tokens/day</th><th>Budget</th><th />
            </tr>
          </thead>
          <tbody>
            {keys.map((key) => (
              <tr key={key.id}>
                <td>{key.name}</td>
                <td>{key.owner}</td>
                <td className="muted">{key.prefix}…</td>
                <td className="num">{formatCount(key.rpm_limit)}</td>
                <td className="num">{formatCount(key.tpd_limit)}</td>
                <td>{key.monthly_budget_usd ? `$${key.monthly_budget_usd}${key.enforce_budget ? " (hard)" : ""}` : "-"}</td>
                <td><button className="btn danger" type="button" onClick={() => revoke(key)}>Revoke</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
