import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Chart } from "../../shared/echarts";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/api";
import type { BacktestRun } from "../../shared/api";
import { backtestEquityOption } from "../../shared/charts";
import { cash, pct, price, timeLabel } from "../../shared/format";
import { Badge, Card, EmptyState, Field, Loader, Stat } from "../../components/ui";
import { ButtonField } from "../../components/form";
import { OptimisePanel } from "./OptimisePanel";
import { WalkForwardPanel } from "./WalkForwardPanel";
import { PortfolioPanel } from "./PortfolioPanel";
import { Segmented } from "../../components/ui";
import { buildParams, initialParams, type ParamValue } from "../../shared/strategy-params";
import { parseStrategyHandoff } from "../../shared/handoff";
import { useAccount } from "../../shared/account-context";

const TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"] as const;

export function BacktestPage() {
  const queryClient = useQueryClient();
  const { account } = useAccount();
  const catalog = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });
  const symbols = useQuery({ queryKey: ["symbols", "all"], queryFn: () => api.symbols() });
  const history = useQuery({ queryKey: ["backtests", 30], queryFn: () => api.backtests(30) });
  const [searchParams, setSearchParams] = useSearchParams();
  // Read the handoff once, before any state is initialised, so the page opens
  // directly on the strategy and parameters chosen in the strategy library.
  const handoff = useRef(parseStrategyHandoff(searchParams, TIMEFRAMES));
  const [strategy, setStrategy] = useState(handoff.current?.strategy ?? "sma_cross");
  const [symbol, setSymbol] = useState(handoff.current?.symbol ?? "BTCUSDT");
  const [timeframe, setTimeframe] = useState<(typeof TIMEFRAMES)[number]>(
    (handoff.current?.timeframe as (typeof TIMEFRAMES)[number] | undefined) ?? "1h",
  );
  const [bars, setBars] = useState("1500");
  const [capital, setCapital] = useState("100000");
  const [positionPct, setPositionPct] = useState("95");
  const [stopLoss, setStopLoss] = useState("0");
  const [takeProfit, setTakeProfit] = useState("0");
  // Seeded from the handoff (empty otherwise) and merged with schema defaults
  // once the catalog resolves.
  const [params, setParams] = useState<Record<string, ParamValue>>(
    handoff.current?.params ?? {},
  );
  const [selectedRun, setSelectedRun] = useState<BacktestRun | null>(null);
  const [tab, setTab] = useState<"run" | "optimise" | "walkforward" | "portfolio">("run");

  // Research is not bound to an account, so every symbol stays selectable — but
  // the default should be an instrument from the account's own market. Opening
  // the page on an A-share account and finding BTCUSDT preselected invites an
  // accidental crypto backtest.
  const handoffSymbol = useRef(handoff.current?.symbol);
  useEffect(() => {
    const rows = symbols.data ?? [];
    if (rows.length === 0 || !account) return;
    // A symbol handed over from the strategy library was chosen deliberately;
    // never replace it, even if it belongs to another market.
    if (handoffSymbol.current) {
      if (rows.some((item) => item.code === handoffSymbol.current)) {
        handoffSymbol.current = undefined;
        return;
      }
      return;
    }
    const inMarket = rows.filter((item) => item.market === account.market);
    if (inMarket.length === 0) return;
    if (inMarket.some((item) => item.code === symbol)) return;
    const preferred = account.market === "cn_stock" ? "600036.SH" : "BTCUSDT";
    setSymbol(
      inMarket.some((item) => item.code === preferred) ? preferred : inMarket[0].code,
    );
  }, [symbols.data, account, symbol]);

  const meta = catalog.data?.find((item) => item.key === strategy) ?? null;

  const metaKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!meta) return;
    // Reset only on a real strategy change; StrictMode's double effect pass
    // must stay idempotent or it wipes the handed-over parameters.
    const switched = metaKeyRef.current !== null && metaKeyRef.current !== meta.key;
    metaKeyRef.current = meta.key;
    setParams((prev) =>
      switched ? initialParams(meta) : { ...initialParams(meta), ...prev },
    );
  }, [meta]);

  useEffect(() => {
    // Strip the query so a refresh or a later strategy switch does not
    // re-apply stale parameters over the user's edits.
    if (!handoff.current) return;
    handoff.current = null;
    setSearchParams({}, { replace: true });
  }, [setSearchParams]);

  const run = useMutation({
    mutationFn: () => {
      const parsedParams = buildParams(meta, params);
      return api.runBacktest({
        strategy,
        symbol,
        timeframe,
        bars: Number(bars),
        params: parsedParams,
        config: {
          initial_capital: Number(capital),
          position_pct: Number(positionPct) / 100,
          stop_loss_pct: Number(stopLoss) / 100,
          take_profit_pct: Number(takeProfit) / 100,
        },
      });
    },
    onSuccess: (result) => {
      setSelectedRun(result);
      queryClient.invalidateQueries({ queryKey: ["backtests"] });
    },
  });

  const deleteRun = useMutation({
    mutationFn: (id: string) => api.deleteBacktest(id),
    onSuccess: () => {
      if (selectedRun) setSelectedRun(null);
      queryClient.invalidateQueries({ queryKey: ["backtests"] });
    },
  });

  const openRun = useMutation({
    mutationFn: (id: string) => api.backtest(id),
    onSuccess: (result) => setSelectedRun(result),
  });

  const metrics = selectedRun?.metrics;
  // The server states the currency; fall back to the symbol suffix only for runs
  // stored before that field existed.
  const runCurrency =
    selectedRun?.currency ??
    (selectedRun?.symbol?.endsWith(".SH") ||
    selectedRun?.symbol?.endsWith(".SZ") ||
    selectedRun?.symbol?.endsWith(".BJ")
      ? "CNY"
      : "USDT");
  const equityOption = useMemo(
    () =>
      selectedRun?.equity
        ? backtestEquityOption(selectedRun.equity, selectedRun.comparison, runCurrency)
        : null,
    [selectedRun, runCurrency],
  );

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <Segmented
          value={tab}
          options={[
            { value: "run", label: "单次回测" },
            { value: "optimise", label: "参数优化" },
            { value: "walkforward", label: "滚动验证" },
            { value: "portfolio", label: "组合分析" },
          ]}
          onChange={(value) => setTab(value)}
        />
        <span className="helper">
          {tab === "run"
            ? "验证一组参数的表现"
            : tab === "optimise"
              ? "枚举参数空间，用样本外数据检验过拟合"
              : tab === "walkforward"
                ? "多窗口滚动：每段独立选参，再看紧接着的样本外表现"
                : "多个回测叠加后的真实风险：相关性、合并波动与分散收益"}
        </span>
      </div>

      {tab === "portfolio" ? (
        <PortfolioPanel history={history.data ?? []} />
      ) : (
      <Card title="目标" subtitle="策略、标的与周期对单次回测和参数优化都生效">
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" }}>
          <Field label="策略">
            <select className="select" value={strategy} onChange={(event) => setStrategy(event.target.value)}>
              {(catalog.data ?? []).map((item) => (
                <option key={item.key} value={item.key}>
                  {item.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="标的">
            <select className="select" value={symbol} onChange={(event) => setSymbol(event.target.value)}>
              {(symbols.data ?? []).map((item) => (
                <option key={item.code} value={item.code}>
                  {item.code}
                </option>
              ))}
            </select>
          </Field>
          <Field label="周期">
            <select
              className="select"
              value={timeframe}
              onChange={(event) => setTimeframe(event.target.value as (typeof TIMEFRAMES)[number])}
            >
              {TIMEFRAMES.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </Card>
      )}

      {tab === "walkforward" ? (
        <WalkForwardPanel
          strategy={strategy}
          symbol={symbol}
          timeframe={timeframe}
          meta={meta}
        />
      ) : null}

      {tab === "optimise" ? (
        <OptimisePanel
          strategy={strategy}
          symbol={symbol}
          timeframe={timeframe}
          meta={meta}
        />
      ) : null}

      {tab === "run" ? (
      <>
      <Card title="回测配置" subtitle="收盘出信号，下一根开盘成交 · 含手续费与滑点">
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))" }}>
          <Field label="K 线数量" hint="50 – 20000">
            <input className="input num" type="number" value={bars} onChange={(event) => setBars(event.target.value)} />
          </Field>
          <Field label="初始资金">
            <input className="input num" type="number" value={capital} onChange={(event) => setCapital(event.target.value)} />
          </Field>
          <Field label="仓位比例 %">
            <input className="input num" type="number" value={positionPct} onChange={(event) => setPositionPct(event.target.value)} />
          </Field>
          <Field label="止损 %" hint="0 表示不启用">
            <input className="input num" type="number" value={stopLoss} onChange={(event) => setStopLoss(event.target.value)} />
          </Field>
          <Field label="止盈 %" hint="0 表示不启用">
            <input className="input num" type="number" value={takeProfit} onChange={(event) => setTakeProfit(event.target.value)} />
          </Field>
          {Object.entries(meta?.params_schema.properties ?? {}).map(([key, schema]) => (
            <Field key={key} label={schema.title ?? key}>
              {schema.type === "boolean" ? (
                <label className="row" style={{ gap: 8, height: 36 }}>
                  <input
                    type="checkbox"
                    checked={Boolean(params[key])}
                    onChange={(event) =>
                      setParams((prev) => ({ ...prev, [key]: event.target.checked }))
                    }
                  />
                  <span className="helper">启用</span>
                </label>
              ) : (
                <input
                  className="input num"
                  type="number"
                  value={String(params[key] ?? "")}
                  onChange={(event) => setParams((prev) => ({ ...prev, [key]: event.target.value }))}
                />
              )}
            </Field>
          ))}
        </div>
        <div className="row" style={{ marginTop: 16, justifyContent: "flex-end" }}>
          <ButtonField disabled={run.isPending} onClick={() => run.mutate()}>
            {run.isPending ? "回测运行中…" : "运行回测"}
          </ButtonField>
        </div>
        {run.error ? <div className="error-text" style={{ marginTop: 8 }}>{(run.error as Error).message}</div> : null}
      </Card>

      {metrics && selectedRun ? (
        <>
          {(metrics.warnings ?? []).length > 0 ? (
            <div
              style={{
                display: "flex",
                gap: 10,
                alignItems: "flex-start",
                padding: "12px 16px",
                borderRadius: 12,
                background: "var(--warning-soft)",
                border: "1px solid #f3ddb8",
                color: "#7a4a06",
                fontSize: 13,
                lineHeight: 1.6,
              }}
            >
              <span style={{ fontWeight: 700 }}>提示</span>
              <div>
                {(metrics.warnings ?? []).map((warn) => (
                  <div key={warn}>{warn}</div>
                ))}
              </div>
            </div>
          ) : null}
          <div className="grid grid-4">
            <Stat
              label="总收益"
              value={pct(metrics.total_return_pct)}
              accent={metrics.total_return_pct >= 0 ? "positive" : "negative"}
              meta={<span className="muted">基准 {pct(metrics.buy_hold_pct)}</span>}
            />
            <Stat
              label="最大回撤"
              value={`${metrics.max_drawdown_pct.toFixed(2)}%`}
              accent="negative"
              meta={<span className="muted">持续 {metrics.max_drawdown_duration_bars} 根</span>}
            />
            <Stat
              label="Sharpe"
              value={metrics.sharpe.toFixed(2)}
              meta={<span className="muted">Sortino {metrics.sortino.toFixed(2)}</span>}
            />
            <Stat
              label="胜率"
              value={`${metrics.win_rate_pct.toFixed(1)}%`}
              meta={
                <span className="muted">
                  {metrics.trade_count} 笔 · 盈亏比 {metrics.profit_factor === null ? "∞" : metrics.profit_factor.toFixed(2)}
                </span>
              }
            />
          </div>

          {metrics.benchmark ? (
            <Card
              title="基准对比"
              subtitle={metrics.benchmark.verdict}
              actions={
                <Badge
                  tone={
                    metrics.benchmark.excess_return_pct >= 0 ? "green" : "red"
                  }
                >
                  超额 {pct(metrics.benchmark.excess_return_pct)}
                </Badge>
              }
            >
              <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))" }}>
                <MetricInline label="策略收益" value={pct(metrics.benchmark.strategy_return_pct)} />
                <MetricInline label="买入持有" value={pct(metrics.benchmark.benchmark_return_pct)} />
                <MetricInline label="Alpha（年化）" value={pct(metrics.benchmark.alpha_pct)} />
                <MetricInline label="Beta" value={metrics.benchmark.beta.toFixed(2)} />
                <MetricInline
                  label="信息比率"
                  value={metrics.benchmark.information_ratio.toFixed(2)}
                />
                <MetricInline
                  label="跟踪误差"
                  value={`${metrics.benchmark.tracking_error_pct.toFixed(1)}%`}
                />
                <MetricInline
                  label="上涨捕获"
                  value={`${metrics.benchmark.up_capture_pct.toFixed(0)}%`}
                />
                <MetricInline
                  label="下跌捕获"
                  value={`${metrics.benchmark.down_capture_pct.toFixed(0)}%`}
                />
              </div>
              <div className="helper" style={{ marginTop: 10 }}>
                超额胜率 {metrics.benchmark.excess_win_rate_pct.toFixed(0)}%（逐根比较跑赢基准的比例）·
                相关系数 {metrics.benchmark.correlation.toFixed(2)}
              </div>
            </Card>
          ) : null}

          <div className="analysis-grid">
            <Card
              title="权益曲线"
              subtitle={`${selectedRun.strategy} · ${selectedRun.symbol} · ${selectedRun.timeframe} · ${selectedRun.bars} 根`}
              actions={<Badge tone="indigo">{selectedRun.duration_ms.toFixed(0)} ms</Badge>}
              padded={false}
            >
              <div style={{ padding: "6px 8px 8px" }}>
                {equityOption ? <Chart option={equityOption} height={320} notMerge /> : null}
              </div>
            </Card>
            <Card title="详细指标" padded={false}>
              <div style={{ padding: "10px 18px 16px" }}>
                <MetricRow label="期末权益" value={cash(metrics.final_equity, runCurrency)} />
                <MetricRow label="年化收益" value={pct(metrics.cagr_pct)} />
                <MetricRow label="年化波动" value={`${metrics.volatility_pct.toFixed(2)}%`} />
                <MetricRow label="Calmar" value={metrics.calmar.toFixed(2)} />
                <MetricRow label="平均每笔" value={pct(metrics.avg_trade_pct, 3)} />
                <MetricRow label="最佳一笔" value={pct(metrics.best_trade_pct, 2)} />
                <MetricRow label="最差一笔" value={pct(metrics.worst_trade_pct, 2)} />
                <MetricRow label="平均持仓" value={`${metrics.avg_hold_bars.toFixed(1)} 根`} />
                <MetricRow label="市场暴露" value={`${metrics.exposure_pct.toFixed(1)}%`} />
                {metrics.blocked_orders ? (
                  <>
                    <MetricRow
                      label="涨停未成交"
                      value={`${metrics.blocked_orders.limit_up} 次`}
                    />
                    <MetricRow
                      label="跌停未成交"
                      value={`${metrics.blocked_orders.limit_down} 次`}
                    />
                    <MetricRow
                      label="T+1 拦截"
                      value={`${metrics.blocked_orders.t_plus_one} 次`}
                    />
                    {metrics.blocked_orders.insufficient_capital ? (
                      <MetricRow
                        label="资金不足一手"
                        value={`${metrics.blocked_orders.insufficient_capital} 次`}
                      />
                    ) : null}
                  </>
                ) : null}
              </div>
            </Card>
          </div>

          <Card title="逐笔交易" subtitle={`${selectedRun.trades?.length ?? 0} 笔已平仓交易`} padded={false}>
            {selectedRun.trades && selectedRun.trades.length > 0 ? (
              <div className="table-wrap" style={{ maxHeight: 360 }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th>入场</th>
                      <th>出场</th>
                      <th className="numeric">入场价</th>
                      <th className="numeric">出场价</th>
                      <th className="numeric">数量</th>
                      <th className="numeric">盈亏</th>
                      <th className="numeric">收益率</th>
                      <th>出场原因</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...selectedRun.trades].reverse().map((trade, index) => (
                      <tr key={index}>
                        <td className="mono muted">{timeLabel(trade.entry_time)}</td>
                        <td className="mono muted">{timeLabel(trade.exit_time)}</td>
                        <td className="numeric">{price(trade.entry_price)}</td>
                        <td className="numeric">{price(trade.exit_price)}</td>
                        <td className="numeric">{trade.quantity}</td>
                        <td className={`numeric ${trade.pnl >= 0 ? "up" : "down"}`}>{cash(trade.pnl, runCurrency)}</td>
                        <td className={`numeric ${trade.pnl_pct >= 0 ? "up" : "down"}`}>{pct(trade.pnl_pct * 100)}</td>
                        <td>
                          <Badge tone={trade.reason === "stop_loss" ? "red" : trade.reason === "take_profit" ? "green" : ""}>
                            {reasonLabel(trade.reason)}
                          </Badge>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="本次回测没有交易" text="尝试放宽参数、延长区间或更换周期。" />
            )}
          </Card>
        </>
      ) : (
        <Card>
          <EmptyState title="配置并运行一次回测" text="结果包含权益曲线、18 项绩效指标与逐笔交易明细，并会自动保存到历史记录。" />
        </Card>
      )}

      </>
      ) : null}

      {tab === "run" ? (
      <Card title="回测历史" subtitle="点击载入结果" padded={false}>
        {history.isLoading ? (
          <Loader />
        ) : history.data && history.data.length > 0 ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>策略</th>
                  <th>标的</th>
                  <th>周期</th>
                  <th className="numeric">K 线</th>
                  <th className="numeric">收益</th>
                  <th className="numeric">回撤</th>
                  <th className="numeric">Sharpe</th>
                  <th className="numeric">耗时</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {history.data.map((item) => (
                  <tr key={item.id} style={{ cursor: "pointer" }} onClick={() => openRun.mutate(item.id)}>
                    <td className="mono muted">{timeLabel(new Date(item.started_at).getTime())}</td>
                    <td style={{ fontWeight: 550 }}>{item.strategy}</td>
                    <td>{item.symbol}</td>
                    <td className="muted">{item.timeframe}</td>
                    <td className="numeric">{item.bars}</td>
                    <td className={`numeric ${item.metrics.total_return_pct >= 0 ? "up" : "down"}`}>
                      {pct(item.metrics.total_return_pct)}
                    </td>
                    <td className="numeric">{item.metrics.max_drawdown_pct.toFixed(2)}%</td>
                    <td className="numeric">{item.metrics.sharpe.toFixed(2)}</td>
                    <td className="numeric muted">{item.duration_ms.toFixed(0)}ms</td>
                    <td>
                      <button
                        className="button small ghost"
                        onClick={(event) => {
                          event.stopPropagation();
                          deleteRun.mutate(item.id);
                        }}
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="还没有回测记录" />
        )}
      </Card>
      ) : null}
    </div>
  );
}

function MetricRow({ label, value }: { label: string; value: string }) {
  return (
    <div
      className="row"
      style={{
        justifyContent: "space-between",
        fontSize: 12.5,
        padding: "7px 0",
        borderBottom: "1px solid var(--border)",
      }}
    >
      <span className="muted">{label}</span>
      <span className="num" style={{ fontWeight: 550 }}>
        {value}
      </span>
    </div>
  );
}

function reasonLabel(reason: string): string {
  const map: Record<string, string> = {
    signal: "策略信号",
    stop_loss: "止损",
    take_profit: "止盈",
    end_of_data: "区间结束",
  };
  return map[reason] ?? reason;
}

function MetricInline({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ padding: "8px 10px", background: "var(--surface-subtle)", borderRadius: 9 }}>
      <div className="helper">{label}</div>
      <div className="num" style={{ fontWeight: 600, fontSize: 14, marginTop: 2 }}>
        {value}
      </div>
    </div>
  );
}
