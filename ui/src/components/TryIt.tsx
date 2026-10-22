import type { GatewayInfo } from "../api";

/** A copy-paste request against this gateway (with the demo key when one is public). */
export function TryIt({ info }: { info: GatewayInfo }) {
  const origin = window.location.origin;
  const key = info.demo_api_key ?? "rk_your_key";
  const model = info.models[0] ?? "relay-mock";
  const curl = [
    `curl ${origin}/v1/chat/completions \\`,
    `  -H "Authorization: Bearer ${key}" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '{"model": "${model}", "messages": [{"role": "user", "content": "Hello, Relay"}]}' -i`,
  ].join("\n");
  return (
    <section className="card" aria-label="Try it">
      <div className="card-head">
        <h2>Try it</h2>
        <span className="muted">v{info.version} · OpenAI-compatible</span>
      </div>
      <p className="secondary" style={{ marginTop: 0 }}>
        {info.demo_api_key
          ? "A rate-limited public demo key against mock providers. Run it twice: the second answer comes from the cache (see the x-relay-cache header)."
          : "Create a key with the admin token, then call the gateway like the OpenAI API."}
      </p>
      <pre className="secret" style={{ whiteSpace: "pre-wrap", margin: 0 }}>{curl}</pre>
      <p className="muted" style={{ marginBottom: 0 }}>
        Models: {info.models.join(", ")} · <a href="/docs">API docs</a>
      </p>
    </section>
  );
}
