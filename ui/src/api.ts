// Typed client for the gateway's admin API (same origin as the dashboard).

export interface Totals {
  requests: number;
  errors: number;
  cache_hits: number;
  semantic_hits: number;
  cache_hit_rate: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  saved_usd: number;
  avg_latency_ms: number;
}

export interface UsageSummary {
  since: string;
  until: string;
  totals: Totals;
  by_day: (Totals & { day: string })[];
  by_key: (Totals & { api_key_id: string; name: string; owner: string })[];
  by_route: (Totals & { route: string })[];
  by_provider: (Totals & { provider: string })[];
}

export type BreakerState = "closed" | "half_open" | "open";

export interface ProvidersView {
  replicas: string[];
  providers: {
    provider: string;
    kind: string;
    configured: boolean;
    worst_state: BreakerState;
    replicas: Record<string, BreakerState>;
    failure_rate: Record<string, number>;
  }[];
  latency_ms: Record<string, Record<string, number>>;
  routes: { model: string; policy: string; targets: string[] }[];
}

export interface UsageEvent {
  request_id: string;
  created_at: string;
  route: string;
  provider: string | null;
  status: string;
  cache: string;
  stream: boolean;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: string;
  latency_ms: number;
  attempts: number;
  replica: string;
}

export interface BudgetAlert {
  api_key_id: string;
  key_name: string;
  owner: string;
  period: string;
  threshold_pct: number;
  spend_usd: string;
  budget_usd: string;
  created_at: string;
}

export interface ApiKey {
  id: string;
  name: string;
  owner: string;
  prefix: string;
  rpm_limit: number;
  tpd_limit: number;
  monthly_budget_usd: string | null;
  enforce_budget: boolean;
  created_at: string;
  revoked_at: string | null;
  active: boolean;
}

export interface CreatedKey extends ApiKey {
  api_key: string;
}

export interface GatewayInfo {
  name: string;
  version: string;
  replica: string;
  models: string[];
  demo_api_key: string | null;
  dashboard_public: boolean;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export class RelayApi {
  constructor(
    private token: string,
    private fetchImpl: typeof fetch = (...args) => fetch(...args),
  ) {}

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (this.token) headers.set("authorization", `Bearer ${this.token}`);
    if (init.body) headers.set("content-type", "application/json");
    const response = await this.fetchImpl(path, { ...init, headers });
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try {
        const body = await response.json();
        message = body?.error?.message ?? message;
      } catch {
        /* not JSON */
      }
      throw new ApiError(response.status, message);
    }
    return (await response.json()) as T;
  }

  summary(days: number, keyId?: string): Promise<UsageSummary> {
    const query = new URLSearchParams({ days: String(days) });
    if (keyId) query.set("key_id", keyId);
    return this.request(`/admin/usage/summary?${query}`);
  }

  info(): Promise<GatewayInfo> {
    return this.request("/info");
  }

  providers(): Promise<ProvidersView> {
    return this.request("/admin/providers");
  }

  events(limit = 20): Promise<UsageEvent[]> {
    return this.request(`/admin/usage/events?limit=${limit}`);
  }

  alerts(): Promise<BudgetAlert[]> {
    return this.request("/admin/alerts?limit=20");
  }

  keys(): Promise<ApiKey[]> {
    return this.request("/admin/keys");
  }

  createKey(body: { name: string; owner: string; rpm_limit: number; tpd_limit: number }): Promise<CreatedKey> {
    return this.request("/admin/keys", { method: "POST", body: JSON.stringify(body) });
  }

  revokeKey(id: string): Promise<ApiKey> {
    return this.request(`/admin/keys/${id}`, { method: "DELETE" });
  }
}
