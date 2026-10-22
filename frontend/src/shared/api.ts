export type Account = {
  id: string;
  name: string;
  kind: "paper" | "live";
  market: "crypto" | "cn_stock";
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
  market: "crypto" | "cn_stock";
  exchange: string;
  board: string;
  base: string;
  quote: string;
  tick_size: number;
  lot_size: number;
  initial_price: number;
  prev_close: number;
  limit_pct: number;
  is_st: boolean;
  suspended: boolean;
  base_timeframe: string;
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
  market: "crypto" | "cn_stock";
  prev_close: number;
  limit_up: number;
  limit_down: number;
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
  broker_order_id: string;
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
  available_quantity: number;
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
  defaults: Record<string, number | string | boolean>;
};

export type SignalPreview = {
  strategy: string;
  symbol: string;
  timeframe: string;
  params: Record<string, number | string>;
  signals: { time: number; price: number; action: "buy" | "sell"; reason: string }[];
  suppressed_signals: number;
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
  market?: string;
  periods_per_year?: number;
  blocked_orders?: {
    limit_up: number;
    limit_down: number;
    t_plus_one: number;
    insufficient_capital?: number;
  };
  warnings?: string[];
  benchmark?: {
    benchmark: string;
    periods: number;
    strategy_return_pct: number;
    benchmark_return_pct: number;
    excess_return_pct: number;
    alpha_pct: number;
    beta: number;
    correlation: number;
    tracking_error_pct: number;
    information_ratio: number;
    up_capture_pct: number;
    down_capture_pct: number;
    excess_win_rate_pct: number;
    strategy_vol_pct: number;
    benchmark_vol_pct: number;
    verdict: string;
  };
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
  currency?: string;
  params: Record<string, number | string>;
  config: Record<string, number | string>;
  metrics: BacktestMetrics;
  initial_capital: number;
  final_equity: number;
  bars: number;
  started_at: string;
  duration_ms: number;
  equity?: { time: number; equity: number }[];
  comparison?: { time: number; strategy: number; benchmark: number }[];
  trades?: BacktestTrade[];
};

export type MultiBacktestComponent = {
  symbol: string;
  name: string;
  share: number;
  allocation: number;
  bars_total: number;
  bars_aligned: number;
  return_pct: number;
  trades: number;
  blocked: Record<string, number>;
};

export type MultiBacktestReport = {
  strategy: string;
  timeframe: string;
  market: "crypto" | "cn_stock";
  currency: string;
  params: Record<string, number | string>;
  initial_capital: number;
  aligned_bars: number;
  rebalance_bars: number;
  rebalance_count: number;
  start_time: number;
  end_time: number;
  metrics: BacktestMetrics;
  equity: { time: number; equity: number }[];
  comparison: { time: number; strategy: number; benchmark: number }[];
  components: MultiBacktestComponent[];
  warnings: string[];
};

export type Alert = {
  id: string;
  account_id: string;
  level: "info" | "warning" | "critical";
  category: string;
  title: string;
  message: string;
  ref_type: string;
  ref_id: string;
  acknowledged: boolean;
  acknowledged_at: string;
  created_at: string;
};

export type AlertSummary = {
  unacknowledged: number;
  by_level: Record<string, number>;
};

export type MultiBacktestSummary = {
  id: string;
  strategy: string;
  symbols: string[];
  timeframe: string;
  market: "crypto" | "cn_stock";
  weights: number[];
  rebalance_bars: number;
  initial_capital: number;
  final_equity: number;
  bars: number;
  metrics: BacktestMetrics;
  child_run_ids?: string[];
  started_at: string;
  duration_ms: number;
};

export type PortfolioComponent = {
  run_id: string;
  strategy: string;
  symbol: string;
  timeframe: string;
  params: Record<string, number | string>;
  weight: number;
  allocation: number;
  bars_total: number;
  bars_aligned: number;
  return_pct: number;
  volatility_pct: number;
  max_drawdown_pct: number;
  trade_count: number;
};

export type PortfolioReport = {
  timeframe: string;
  market: "crypto" | "cn_stock";
  currency: string;
  initial_capital: number;
  start_time: number;
  end_time: number;
  aligned_bars: number;
  metrics: BacktestMetrics;
  equity: { time: number; equity: number }[];
  components: PortfolioComponent[];
  correlation: {
    labels: string[];
    matrix: number[][];
    // Present only when a sleeve had an impossible bar; the primary matrix is
    // then known to be distorted and this one excludes those bars.
    robust?: {
      matrix: number[][];
      dropped_bars: number;
      returns_per_sleeve: number;
    };
  };
  diversification: {
    weighted_volatility_pct: number;
    combined_volatility_pct: number;
    ratio: number;
    benefit_pct: number;
  };
  warnings: string[];
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
  stop_loss_pct: number;
  take_profit_pct: number;
  trailing_stop_pct: number;
  peak_price: number;
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

export type CnRules = {
  lot_size: number;
  boards: Record<string, { limit_pct: number }>;
  st_limit_pct: number;
  t_plus_one: boolean;
  settlement: string;
  sessions: { open: string; close: string }[];
  fees: {
    commission_bps: number;
    min_commission: number;
    stamp_duty_bps: number;
    transfer_fee_bps: number;
  };
  session_enforced_for_paper: boolean;
  symbols: number;
};

export type CnSession = {
  simulated_now: number;
  speed: number;
  open: boolean;
  day: string;
  session: string;
  session_open: number;
  session_close: number;
  next_open: number;
};

export type CalendarCoverage = {
  year: number;
  covered: boolean;
  covered_years: number[];
  warning: string;
};

export type CnCalendar = {
  today: string;
  is_trading_day: boolean;
  prev_trading_day: string;
  next_trading_day: string;
  upcoming: string[];
  coverage: CalendarCoverage;
  upcoming_coverage: CalendarCoverage;
};

export type CnSymbol = SymbolInfo & {
  price: number;
  change_pct: number;
  limit_up: number;
  limit_down: number;
};

export type CryptoQuoteHealth = {
  source: string;
  mode: string;
  poll_seconds: number;
  symbols: number;
  last_poll_age_ms: number;
  worst_age_ms: number;
  latency_ms: number;
  last_error: string;
};

export type CryptoLiveQuote = {
  code: string;
  price: number;
  prev_close: number;
  open: number;
  high: number;
  low: number;
  volume: number;
  timestamp: number;
  age_ms: number;
  fresh: boolean;
};

export type CnQuoteHealth = {
  source: string;
  mode: string;
  poll_seconds: number;
  quotes: number;
  last_poll: number;
  last_poll_age_ms: number;
  worst_age_ms: number;
  latency_ms: number;
  last_error: string;
  session_open: boolean;
};

export type CnLiveQuote = {
  code: string;
  price: number;
  prev_close: number;
  open: number;
  high: number;
  low: number;
  volume: number;
  timestamp: number;
  fetched_at: number;
  age_ms: number;
  fresh: boolean;
  source: string;
};

export type ThsPosition = {
  symbol: string;
  quantity: number;
  available_quantity: number;
  avg_price: number;
};

export type ThsHealth = {
  ok: boolean;
  gateway: string;
  can_trade: boolean;
  balances: Record<string, number>;
};

export type ThsReconcile = {
  account_id: string;
  gateway: string;
  all_match: boolean;
  positions: {
    symbol: string;
    local_quantity: number;
    gateway_quantity: number;
    local_available: number;
    gateway_available: number;
    match: boolean;
  }[];
};

export type BrokerChannels = {
  live_trading_enabled: boolean;
  channels: Record<
    string,
    {
      market: string;
      ready: boolean;
      credentials_configured?: boolean;
      gateway_configured?: boolean;
      mock_enabled?: boolean;
    }
  >;
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
  day_key: string;
  halt_kind: "" | "daily" | "drawdown";
};

export type RiskOverview = {
  account: { id: string; name: string; market: string; base_currency: string; kind: string };
  state: RiskState;
  limits: RiskLimits;
  exposure: {
    equity: number;
    cash: number;
    available_cash: number;
    reserved_cash: number;
    positions_value: number;
    exposure_pct: number;
    position_count: number;
    largest_position_pct: number;
  };
  positions: {
    symbol: string;
    quantity: number;
    available_quantity: number;
    avg_price: number;
    market_price: number;
    market_value: number;
    unrealized_pnl: number;
    unrealized_pct: number;
  }[];
  instances: {
    id: string;
    strategy: string;
    symbol: string;
    running: boolean;
    stop_loss_pct: number;
    take_profit_pct: number;
    trailing_stop_pct: number;
    peak_price: number;
    executions: number;
    last_error: string;
    protected: boolean;
  }[];
  warnings: string[];
};

export type BrokerStatus = {
  live_trading_enabled: boolean;
  broker: string;
  credentials_configured: boolean;
  ready: boolean;
};

export type Analytics = {
  account_id: string;
  samples: number;
  start_equity: number;
  current_equity: number;
  total_return_pct: number;
  max_drawdown_pct: number;
  max_drawdown_samples: number;
  volatility_pct: number;
  sharpe_per_sample: number;
  sortino_per_sample: number;
  positive_return_pct: number;
  profit_factor: number | null;
  best_sample_pct: number;
  worst_sample_pct: number;
  attribution: {
    symbol: string;
    market_value: number;
    unrealized_pnl: number;
    realized_pnl: number;
    total_pnl: number;
  }[];
};

export type SystemMetrics = {
  requests: {
    uptime_seconds: number;
    requests: number;
    server_errors: number;
    error_rate: number;
    status_codes: Record<string, number>;
    slowest_routes: [string, { count: number; errors: number; avg_ms: number; max_ms: number }][];
  };
  caches: Record<string, { entries: number; hits: number; misses: number; hit_rate: number }>;
  engines: Record<string, boolean>;
};

export type AuditEntry = {
  id: string;
  actor: string;
  action: string;
  entity: string;
  entity_id: string;
  detail: Record<string, unknown>;
  created_at: string;
};

export type OptimisationCandidate = {
  params: Record<string, number | boolean | string>;
  total_return_pct: number;
  max_drawdown_pct: number;
  sharpe: number;
  trade_count: number;
  win_rate_pct: number;
  profit_factor: number | null;
  final_equity: number;
  score: number;
  in_sample_return_pct: number;
  out_of_sample_return_pct: number;
  rejected: boolean;
  reject_reason: string;
};

export type OptimisationReport = {
  strategy: string;
  symbol: string;
  timeframe: string;
  objective: string;
  bars: number;
  tested: number;
  valid: number;
  rejected: number;
  elapsed_ms: number;
  best: OptimisationCandidate | null;
  top: OptimisationCandidate[];
  robust_alternatives: OptimisationCandidate[];
  distribution: {
    profitable_count?: number;
    profitable_pct?: number;
    median_return_pct?: number;
    best_return_pct?: number;
    worst_return_pct?: number;
    spread_pct?: number;
  };
  warnings: string[];
  data_source: string;
  in_sample_bars: number;
  out_of_sample_bars: number;
};

export type WalkForwardWindow = {
  index: number;
  train_start: number;
  train_end: number;
  test_start: number;
  test_end: number;
  params: Record<string, number | boolean | string>;
  train_return_pct: number;
  test_return_pct: number;
  test_drawdown_pct: number;
  test_trades: number;
  test_sharpe: number;
  degradation_pct: number;
};

export type WalkForwardReport = {
  strategy: string;
  symbol: string;
  timeframe: string;
  objective: string;
  total_bars: number;
  windows: WalkForwardWindow[];
  aggregate: {
    windows_completed?: number;
    requested_windows?: number;
    skipped_windows?: number;
    positive_windows?: number;
    positive_pct?: number;
    mean_test_return_pct?: number;
    median_test_return_pct?: number;
    worst_test_return_pct?: number;
    best_test_return_pct?: number;
    std_test_return_pct?: number;
    compounded_test_return_pct?: number;
    mean_degradation_pct?: number;
    mean_test_trades?: number;
    mean_test_drawdown_pct?: number;
  };
  warnings: string[];
  elapsed_ms: number;
  data_source: string;
};

export type Overview = {
  account_id: string;
  account_name: string;
  base_currency: string;
  initial_balance: number;
  net_contributions: number;
  cash: number;
  available_cash: number;
  reserved_cash: number;
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
  net_contributions: number;
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
    market?: "crypto" | "cn_stock";
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

  symbols: (market?: "crypto" | "cn_stock") =>
    request<SymbolInfo[]>(`/market/symbols${qs({ market })}`),
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
  snapshots: (market?: "crypto" | "cn_stock") =>
    request<Snapshot[]>(`/market/snapshots${qs({ market })}`),
  snapshot: (symbol: string) => request<Snapshot>(`/market/snapshot${qs({ symbol })}`),
  clock: () =>
    request<{
      simulated_now: number;
      speed: number;
      timeframe: string;
      tick_seconds: number;
      cn?: {
        open: boolean;
        day: string;
        session: string;
        next_open: number;
        simulated?: boolean;
        source?: string;
      };
    }>("/market/clock"),

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
    stop_loss_pct?: number;
    take_profit_pct?: number;
    trailing_stop_pct?: number;
    start: boolean;
  }) => request<StrategyInstance>("/runtime/instances", { method: "POST", body: JSON.stringify(body) }),
  updateRuntimeInstance: (
    id: string,
    body: {
      params?: Record<string, unknown>;
      position_pct?: number;
      max_order_notional?: number;
      stop_loss_pct?: number;
      take_profit_pct?: number;
      trailing_stop_pct?: number;
    },
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
  // Positional parameters like every other list endpoint (`orders`, `fills`,
  // `backtests`). An options object here was the odd one out and made both the
  // call sites and their mocks harder to read.
  alerts: (
    accountId?: string,
    unacknowledgedOnly = false,
    limit = 100,
    level?: string,
    category?: string,
  ) =>
    request<Alert[]>(
      `/alerts${qs({
        account_id: accountId,
        unacknowledged_only: unacknowledgedOnly,
        level,
        category,
        limit,
      })}`,
    ),
  alertCategories: (accountId?: string) =>
    request<{ category: string; count: number }[]>(
      `/alerts/categories${qs({ account_id: accountId })}`,
    ),
  alertSummary: (accountId?: string) =>
    request<AlertSummary>(`/alerts/summary${qs({ account_id: accountId })}`),
  ackAlert: (id: string) => request<Alert>(`/alerts/${id}/ack`, { method: "POST" }),
  ackAllAlerts: (accountId?: string) =>
    request<{ acknowledged: number }>(`/alerts/ack-all${qs({ account_id: accountId })}`, {
      method: "POST",
    }),
  multiBacktests: (limit = 30) =>
    request<MultiBacktestSummary[]>(`/backtests/multi${qs({ limit })}`),
  multiBacktestRun: (id: string) =>
    request<MultiBacktestReport & { id: string }>(`/backtests/multi/${id}`),
  deleteMultiBacktest: (id: string) =>
    request<void>(`/backtests/multi/${id}`, { method: "DELETE" }),
  multiBacktest: (body: {
    strategy: string;
    symbols: string[];
    timeframe: string;
    bars: number;
    params: Record<string, unknown>;
    config: Record<string, unknown>;
    weights?: number[];
    rebalance_bars?: number;
  }) =>
    request<MultiBacktestReport>("/backtests/multi", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  portfolioBacktest: (body: {
    run_ids: string[];
    weights?: number[];
    initial_capital?: number;
  }) =>
    request<PortfolioReport>("/backtests/portfolio", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  applyPortfolioWeights: (body: { run_ids: string[]; weights: number[] }) =>
    request<MultiBacktestReport & { id: string; previous_run_id: string }>(
      "/backtests/portfolio/apply-weights",
      { method: "POST", body: JSON.stringify(body) },
    ),
  walkForwardBacktest: (body: {
    strategy: string;
    symbol: string;
    timeframe: string;
    bars: number;
    space: Record<string, (number | boolean)[]>;
    base_params?: Record<string, unknown>;
    objective: string;
    config?: Record<string, unknown>;
    windows: number;
  }) => request<WalkForwardReport>("/backtests/walk-forward", { method: "POST", body: JSON.stringify(body) }),
  optimiseBacktest: (body: {
    strategy: string;
    symbol: string;
    timeframe: string;
    bars: number;
    space: Record<string, (number | boolean)[]>;
    base_params?: Record<string, unknown>;
    objective: string;
    config?: Record<string, unknown>;
    top_n?: number;
  }) => request<OptimisationReport>("/backtests/optimise", { method: "POST", body: JSON.stringify(body) }),

  portfolioOverview: (accountId?: string) =>
    request<Overview>(`/portfolio/overview${qs({ account_id: accountId })}`),
  portfolioAnalytics: (accountId?: string, limit = 1000) =>
    request<Analytics>(`/portfolio/analytics${qs({ account_id: accountId, limit })}`),
  portfolioHistory: (accountId?: string, limit = 500) =>
    request<EquityPoint[]>(`/portfolio/history${qs({ account_id: accountId, limit })}`),

  riskLimits: () => request<RiskLimits>("/risk/limits"),
  riskState: (accountId: string) =>
    request<RiskState>(`/risk/state${qs({ account_id: accountId })}`),
  riskOverview: (accountId: string) =>
    request<RiskOverview>(`/risk/overview${qs({ account_id: accountId })}`),
  resetRisk: (accountId: string) =>
    request<RiskState>(`/risk/reset${qs({ account_id: accountId })}`, { method: "POST" }),
  brokerStatus: () => request<BrokerStatus>("/brokers/status"),

  dataCoverage: () =>
    request<{ symbol: string; timeframe: string; bars: number; first_time: number; last_time: number }[]>(
      "/data/coverage",
    ),
  dataQuality: (symbol: string, timeframe?: string) =>
    request<{
      symbol: string;
      timeframe: string;
      bars: number;
      first_time: number;
      last_time: number;
      duplicate_times: number;
      non_monotonic: number;
      invalid_ohlc: number;
      gaps: number;
      expected_step: number;
    }>(`/data/quality${qs({ symbol, timeframe })}`),
  deleteSeries: (symbol: string, timeframe: string) =>
    request<{ symbol: string; timeframe: string; deleted: number }>(
      `/data/series${qs({ symbol, timeframe })}`,
      { method: "DELETE" },
    ),
  importCsv: async (symbol: string, file: File, replace: boolean, timeframe = "15m") => {
    const form = new FormData();
    form.set("symbol", symbol);
    form.set("timeframe", timeframe);
    form.set("replace", String(replace));
    form.set("file", file);
    const response = await fetch("/api/data/import", { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok) {
      throw new ApiError(
        response.status,
        payload?.error?.code ?? "import_failed",
        payload?.error?.message ?? "导入失败",
      );
    }
    return payload as { symbol: string; timeframe: string; imported: number; skipped: number; errors: string[] };
  },

  systemMetrics: () => request<SystemMetrics>("/system/metrics"),
  audit: (limit = 100, action?: string) =>
    request<AuditEntry[]>(`/system/audit${qs({ limit, action })}`),
  auditSummary: () => request<{ action: string; count: number }[]>("/system/audit/summary"),

  cnRules: () => request<CnRules>("/cn/rules"),
  cnSession: () => request<CnSession>("/cn/session"),
  cnCalendar: (days = 20) => request<CnCalendar>(`/cn/calendar${qs({ days })}`),
  cnSymbols: () => request<CnSymbol[]>("/cn/symbols"),
  suspendSymbol: (code: string, suspended: boolean) =>
    request<SymbolInfo>(`/cn/symbols/${code}/suspend${qs({ suspended })}`, { method: "POST" }),
  settleT1: () => request<{ unlocked: number }>("/trading/settle", { method: "POST" }),
  cnDataSource: () =>
    request<{
      configured: string;
      available: boolean;
      detail: string;
      latency_ms: number;
      mode?: "synthetic" | "on-demand" | "realtime-poll";
      poll_seconds?: number;
      quotes?: number;
      last_poll_age_ms?: number;
      session_open?: boolean;
    }>("/cn/data/source"),
  cnQuotes: () =>
    request<{ health: CnQuoteHealth; quotes: CnLiveQuote[] }>("/cn/data/quotes"),
  cnPollOnce: () =>
    request<{ updated: number; health: CnQuoteHealth }>("/cn/data/poll", { method: "POST" }),
  cryptoDataSource: () =>
    request<{
      configured: string;
      mode: "synthetic" | "realtime-poll";
      available: boolean;
      detail: string;
      latency_ms: number;
      poll_seconds?: number;
      symbols?: number;
      last_poll_age_ms?: number;
    }>("/market/data/source"),
  cryptoQuotes: () =>
    request<{ health: CryptoQuoteHealth; quotes: CryptoLiveQuote[] }>("/market/data/quotes"),
  cnSync: (codes?: string) =>
    request<{
      source: string;
      ok: boolean;
      elapsed_ms: number;
      symbols: {
        code: string;
        daily_bars: number;
        intraday_bars: number;
        quote_price: number;
        prev_close: number;
        error: string;
      }[];
    }>(`/cn/data/sync${qs({ codes })}`, { method: "POST" }),
  cnStaleness: () =>
    request<{ code: string; name: string; latest_daily: number; stale_days: number }[]>(
      "/cn/data/staleness",
    ),
  brokerChannels: () => request<BrokerChannels>("/brokers/status"),
  thsHealth: () => request<ThsHealth>("/brokers/ths/health"),
  thsPositions: () => request<ThsPosition[]>("/brokers/ths/positions"),
  thsReconcile: (accountId: string) =>
    request<ThsReconcile>(`/brokers/ths/reconcile${qs({ account_id: accountId })}`),

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
