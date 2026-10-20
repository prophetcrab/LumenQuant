export type Account = {
  id: string;
  name: string;
  kind: "paper" | "live";
  broker: string;
  base_currency: string;
  initial_balance: number;
  leverage: number;
  status: "active" | "archived";
  cash_balance: number;
  created_at: string;
  updated_at: string;
};

export type LedgerEntry = {
  id: string;
  account_id: string;
  kind: string;
  amount: number;
  balance_after: number;
  ref: string;
  note: string;
  created_at: string;
};

export type SymbolInfo = {
  code: string;
  name: string;
  asset_class: string;
  base: string;
  quote: string;
  tick_size: number;
  lot_size: number;
  initial_price: number;
  enabled: boolean;
};

export type Snapshot = {
  symbol: string;
  price: number;
  change_24h_pct: number;
  high_24h: number;
  low_24h: number;
  volume_24h: number;
  time: number;
};

export type Candle = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type CandleResponse = {
  symbol: string;
  timeframe: string;
  candles: Candle[];
};

export type IndicatorResponse = {
  symbol: string;
  timeframe: string;
  times: number[];
  series: Record<string, (number | null)[]>;
};

export type Order = {
  id: string;
  account_id: string;
  symbol: string;
  side: "buy" | "sell";
  order_type: "market" | "limit";
  quantity: number;
  limit_price: number | null;
  filled_quantity: number;
  avg_fill_price: number;
  fee: number;
  status: "new" | "open" | "partially_filled" | "filled" | "cancelled" | "rejected";
  source: string;
  client_order_id: string | null;
  reject_reason: string;
  created_at: string;
  updated_at: string;
};

export type Fill = {
  id: string;
  order_id: string;
  account_id: string;
  symbol: string;
  side: "buy" | "sell";
  quantity: number;
  price: number;
  fee: number;
  created_at: string;
};

export type Position = {
  account_id: string;
  symbol: string;
  quantity: number;
  avg_price: number;
  realized_pnl: number;
  market_price: number;
  market_value: number;
  unrealized_pnl: number;
  updated_at: string;
};

export type StrategyMeta = {
  key: string;
  name: string;
  category: string;
  description: string;
  tags: string[];
  params_schema: {
    type: string;
    properties?: Record<
      string,
      {
        type?: string;
        title?: string;
        default?: number | string;
        minimum?: number;
        maximum?: number;
        description?: string;
      }
    >;
    required?: string[];
  };
  defaults: Record<string, number | string>;
};

export type SignalPreview = {
  strategy: string;
  symbol: string;
  timeframe: string;
  params: Record<string, number | string>;
  signals: { time: number; price: number; action: "buy" | "sell"; reason: string }[];
  overlays: Record<string, (number | null)[]>;
};

export type BacktestMetrics = {
  initial_capital: number;
  final_equity: number;
  total_return_pct: number;
  cagr_pct: number;
  max_drawdown_pct: number;
  max_drawdown_duration_bars: number;
  volatility_pct: number;
  sharpe: number;
  sortino: number;
  calmar: number;
  trade_count: number;
  win_rate_pct: number;
  profit_factor: number | null;
  avg_trade_pct: number;
  best_trade_pct: number;
  worst_trade_pct: number;
  avg_hold_bars: number;
  exposure_pct: number;
  bars: number;
  buy_hold_pct: number;
};

export type BacktestTrade = {
  entry_time: number;
  exit_time: number;
  entry_price: number;
  exit_price: number;
  quantity: number;
  pnl: number;
  pnl_pct: number;
  fee: number;
  reason: string;
};

export type BacktestRun = {
  id: string;
  strategy: string;
  symbol: string;
  timeframe: string;
  params: Record<string, number | string>;
  config: Record<string, number | string>;
  metrics: BacktestMetrics;
  initial_capital: number;
  final_equity: number;
  bars: number;
  started_at: string;
  duration_ms: number;
  equity?: { time: number; equity: number }[];
  trades?: BacktestTrade[];
};

export type StrategyInstance = {
  id: string;
  account_id: string;
  strategy_key: string;
  strategy_name: string;
  symbol: string;
  timeframe: string;
  params: Record<string, number | string>;
  position_pct: number;
  max_order_notional: number;
  running: boolean;
  last_bar_time: number;
  last_signal_at: string;
  last_error: string;
  executions: number;
  created_at: string;
  updated_at: string;
};

export type RuntimeLog = {
  id: string;
  instance_id: string;
  level: "info" | "warn" | "error";
  event: string;
  message: string;
  order_id: string;
  bar_time: number;
  created_at: string;
};

export type AssistantTool = {
  name: string;
  description: string;
  parameters: Record<string, string>;
};

export type AssistantReply = {
  answer: string;
  mode: "local" | "llm";
  tool_calls: { tool: string; arguments: Record<string, unknown>; result: unknown }[];
};

export type RiskLimits = {
  max_order_notional: number;
  max_position_notional: number;
  min_order_notional: number;
  max_orders_per_minute: number;
  max_daily_loss_pct: number;
  max_drawdown_pct: number;
};

export type RiskState = {
  account_id: string;
  day_start_equity: number;
  peak_equity: number;
  breaker_active: boolean;
  breaker_reason: string;
  updated_at: string;
};

export type BrokerStatus = {
  live_trading_enabled: boolean;
  broker: string;
  credentials_configured: boolean;
  ready: boolean;
};

export type Overview = {
  account_id: string;
  account_name: string;
  base_currency: string;
  initial_balance: number;
  net_contributions: number;
  cash: number;
  positions_value: number;
  equity: number;
  total_return_pct: number;
  realized_pnl: number;
  unrealized_pnl: number;
  exposure_pct: number;
  position_count: number;
  allocations: {
    symbol: string;
    market_value: number;
    weight_pct: number;
    unrealized_pnl: number;
  }[];
  change_pct: number;
  updated_at: string;
};

export type EquityPoint = {
  time: string;
  equity: number;
  cash: number;
  positions_value: number;
};

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const message = payload?.error?.message ?? payload?.detail?.[0]?.msg ?? response.statusText;
    const code = payload?.error?.code ?? "request_failed";
    throw new ApiError(response.status, code, message);
  }
  return payload as T;
}

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  });
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  accounts: () => request<Account[]>("/accounts"),
  createAccount: (body: {
    name: string;
    initial_balance: number;
    leverage?: number;
    kind?: "paper" | "live";
  }) => request<Account>("/accounts", { method: "POST", body: JSON.stringify(body) }),
  updateAccount: (id: string, body: { name?: string; status?: string; leverage?: number }) =>
    request<Account>(`/accounts/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deposit: (id: string, amount: number, note = "") =>
    request<Account>(`/accounts/${id}/deposit`, {
      method: "POST",
      body: JSON.stringify({ amount, note }),
    }),
  withdraw: (id: string, amount: number, note = "") =>
    request<Account>(`/accounts/${id}/withdraw`, {
      method: "POST",
      body: JSON.stringify({ amount, note }),
    }),
  ledger: (id: string, limit = 100) =>
    request<LedgerEntry[]>(`/accounts/${id}/ledger${qs({ limit })}`),

  symbols: () => request<SymbolInfo[]>("/market/symbols"),
  createSymbol: (body: {
    code: string;
    name?: string;
    initial_price: number;
    base?: string;
    quote?: string;
  }) => request<SymbolInfo>("/market/symbols", { method: "POST", body: JSON.stringify(body) }),
  setSymbolEnabled: (code: string, enabled: boolean) =>
    request<SymbolInfo>(`/market/symbols/${code}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  candles: (symbol: string, timeframe: string, limit = 300) =>
    request<CandleResponse>(`/market/candles${qs({ symbol, timeframe, limit })}`),
  indicators: (symbol: string, timeframe: string, limit: number, specs: string) =>
    request<IndicatorResponse>(`/market/indicators${qs({ symbol, timeframe, limit, specs })}`),
  snapshots: () => request<Snapshot[]>("/market/snapshots"),
  snapshot: (symbol: string) => request<Snapshot>(`/market/snapshot${qs({ symbol })}`),
  clock: () =>
    request<{ simulated_now: number; speed: number; timeframe: string; tick_seconds: number }>(
      "/market/clock",
    ),

  orders: (accountId: string, status?: string, limit = 200) =>
    request<Order[]>(`/trading/orders${qs({ account_id: accountId, status, limit })}`),
  placeOrder: (body: {
    account_id: string;
    symbol: string;
    side: "buy" | "sell";
    order_type: "market" | "limit";
    quantity: number;
    limit_price?: number;
    client_order_id?: string;
  }) => request<Order>("/trading/orders", { method: "POST", body: JSON.stringify(body) }),
  cancelOrder: (orderId: string, accountId: string) =>
    request<Order>(`/trading/orders/${orderId}${qs({ account_id: accountId })}`, {
      method: "DELETE",
    }),
  positions: (accountId: string) => request<Position[]>(`/trading/positions${qs({ account_id: accountId })}`),
  fills: (accountId: string, limit = 100) =>
    request<Fill[]>(`/trading/fills${qs({ account_id: accountId, limit })}`),
  closePosition: (accountId: string, symbol: string) =>
    request<Order | null>(`/trading/positions/${symbol}/close`, {
      method: "POST",
      body: JSON.stringify({ account_id: accountId }),
    }),

  strategies: () => request<StrategyMeta[]>("/strategies"),
  strategySignals: (
    key: string,
    body: { symbol: string; timeframe: string; limit: number; params: Record<string, unknown> },
  ) => request<SignalPreview>(`/strategies/${key}/signals`, { method: "POST", body: JSON.stringify(body) }),

  runtimeInstances: (accountId?: string) =>
    request<StrategyInstance[]>(`/runtime/instances${qs({ account_id: accountId })}`),
  createRuntimeInstance: (body: {
    account_id: string;
    strategy_key: string;
    symbol: string;
    timeframe: string;
    params: Record<string, unknown>;
    position_pct: number;
    max_order_notional: number;
    start: boolean;
  }) => request<StrategyInstance>("/runtime/instances", { method: "POST", body: JSON.stringify(body) }),
  updateRuntimeInstance: (
    id: string,
    body: { params?: Record<string, unknown>; position_pct?: number; max_order_notional?: number },
  ) => request<StrategyInstance>(`/runtime/instances/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  toggleRuntimeInstance: (id: string, running: boolean) =>
    request<StrategyInstance>(`/runtime/instances/${id}/toggle`, {
      method: "POST",
      body: JSON.stringify({ running }),
    }),
  deleteRuntimeInstance: (id: string) =>
    request<void>(`/runtime/instances/${id}`, { method: "DELETE" }),
  runtimeLogs: (id: string, limit = 100) =>
    request<RuntimeLog[]>(`/runtime/instances/${id}/logs${qs({ limit })}`),
  runtimeTick: () => request<{ evaluated: Record<string, string> }>("/runtime/tick", { method: "POST" }),

  backtests: (limit = 30) => request<BacktestRun[]>(`/backtests${qs({ limit })}`),
  backtest: (id: string) => request<BacktestRun>(`/backtests/${id}`),
  runBacktest: (body: {
    strategy: string;
    symbol: string;
    timeframe: string;
    bars: number;
    params: Record<string, unknown>;
    config: Record<string, unknown>;
  }) => request<BacktestRun>("/backtests", { method: "POST", body: JSON.stringify(body) }),
  deleteBacktest: (id: string) => request<void>(`/backtests/${id}`, { method: "DELETE" }),

  portfolioOverview: (accountId?: string) =>
    request<Overview>(`/portfolio/overview${qs({ account_id: accountId })}`),
  portfolioHistory: (accountId?: string, limit = 500) =>
    request<EquityPoint[]>(`/portfolio/history${qs({ account_id: accountId, limit })}`),

  riskLimits: () => request<RiskLimits>("/risk/limits"),
  riskState: (accountId: string) =>
    request<RiskState>(`/risk/state${qs({ account_id: accountId })}`),
  resetRisk: (accountId: string) =>
    request<RiskState>(`/risk/reset${qs({ account_id: accountId })}`, { method: "POST" }),
  brokerStatus: () => request<BrokerStatus>("/brokers/status"),

  assistantStatus: () =>
    request<{ mode: "local" | "llm"; model: string }>("/assistant/status"),
  assistantTools: () => request<AssistantTool[]>("/assistant/tools"),
  assistantChat: (message: string, accountId?: string) =>
    request<AssistantReply>("/assistant/chat", {
      method: "POST",
      body: JSON.stringify({ message, account_id: accountId }),
    }),

  meta: () => request<{ modules: string[]; capabilities: Record<string, boolean> }>("/meta"),
};

export async function health(): Promise<{ status: string; version: string; database: string }> {
  const response = await fetch("/health");
  if (!response.ok) throw new ApiError(response.status, "health_failed", "health check failed");
  return response.json();
}
