import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Chart } from "../../shared/echarts";
import { api } from "../../shared/api";
import type { MultiBacktestReport, StrategyMeta } from "../../shared/api";
import { backtestEquityOption } from "../../shared/charts";
import { cash, pct } from "../../shared/format";
import { Badge, Card, EmptyState, Field, Stat } from "../../components/ui";
import { ButtonField } from "../../components/form";
import { buildParams, initialParams, type ParamValue } from "../../shared/strategy-params";
import type { Timeframe } from "../../shared/timeframes";

type Props = {
  strategy: string;
  timeframe: Timeframe;
  meta: StrategyMeta | null;
  onStrategyChange: (key: string) => void;
  onTimeframeChange: (value: Timeframe) => void;
  timeframeOptions: readonly Timeframe[];
};

const MAX_SYMBOLS = 12;

const mag = (value: number) => `${value.toFixed(2)}%`;

export function MultiSymbolPanel({
  strategy,
  timeframe,
  meta,
  onStrategyChange,
  onTimeframeChange,
  timeframeOptions,
}: Props) {
  const catalog = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });
  const symbols = useQuery({ queryKey: ["symbols", "all"], queryFn: () => api.symbols() });
  const [selected, setSelected] = useState<string[]>([]);
  const [weights, setWeights] = useState<Record<string, string>>({});
  const [bars, setBars] = useState("300");
  const [capital, setCapital] = useState("300000");
  // 0 = let each sleeve compound on its own; >0 = restore target weights.
  const [rebalance, setRebalance] = useState("0");
  const [params, setParams] = useState<Record<string, ParamValue>>(() =>
    initialParams(meta),
  );

  // Reset the parameter form when the strategy changes, matching the other tabs.
  const metaKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!meta) return;
    const switched = metaKeyRef.current !== null && metaKeyRef.current !== meta.key;
    metaKeyRef.current = meta.key;
    setParams((prev) =>
      switched ? initialParams(meta) : { ...initialParams(meta), ...prev },
    );
  }, [meta]);

  const marketOf = (code: string) => symbols.data?.find((s) => s.code === code)?.market;

  const toggle = (code: string) => {
    setSelected((prev) => {
      if (prev.includes(code)) {
        const next = prev.filter((c) => c !== code);
        setWeights((current) => {
          const copy = { ...current };
          delete copy[code];
          return copy;
        });
        return next;
      }
      if (prev.length >= MAX_SYMBOLS) return prev;
      return [...prev, code];
    });
  };

  const run = useMutation({
    mutationFn: () => {
      const payloadWeights = selected.map((code) => {
        const raw = Number(weights[code]);
        return Number.isFinite(raw) && raw > 0 ? raw : 1;
      });
      const uniform = payloadWeights.every((w) => w === payloadWeights[0]);
      return api.multiBacktest({
        strategy,
        symbols: selected,
        timeframe,
        bars: Number(bars),
        params: buildParams(meta, params),
        config: { initial_capital: Number(capital) },
        weights: uniform ? undefined : payloadWeights,
        rebalance_bars: Math.max(0, Number(rebalance) || 0),
      });
    },
  });

  const report = run.data ?? null;
  const currency = report?.currency ?? "USDT";
  const equityOption = useMemo(
    () =>
      report
        ? backtestEquityOption(report.equity, report.comparison, currency)
        : null,
    [report, currency],
  );

  // A basket must share one market; the backend rejects mixtures, so the picker
  // narrows itself to the first selection's market instead of failing later.
  const lockedMarket = selected.length > 0 ? marketOf(selected[0]) : undefined;
  const available = (symbols.data ?? []).filter(
    (item) => !lockedMarket || item.market === lockedMarket,
  );

  return (
    <div className="stack" style={{ gap: 16 }}>
      <Card
        title="多标的回测"
        subtitle="同一策略跑一篮子标的，资金按权重分配，各自独立交易"
        actions={
          <ButtonField
            disabled={selected.length < 2 || run.isPending}
            onClick={() => run.mutate()}
          >
            {run.isPending ? "计算中…" : `回测 ${selected.length} 个标的`}
          </ButtonField>
        }
      >
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", marginBottom: 14 }}>
          <Field label="策略">
            <select
              className="select"
              value={strategy}
              onChange={(event) => onStrategyChange(event.target.value)}
            >
              {(catalog.data ?? []).map((item) => (
                <option key={item.key} value={item.key}>
                  {item.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="周期">
            <select
              className="select"
              value={timeframe}
              onChange={(event) => onTimeframeChange(event.target.value as Timeframe)}
            >
              {timeframeOptions.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </Field>
          <Field label="K 线数量" hint="每个标的各自取这么多根">
            <input className="input num" type="number" value={bars} onChange={(e) => setBars(e.target.value)} />
          </Field>
          <Field label="组合总资金" hint="按权重拆分到各标的">
            <input className="input num" type="number" value={capital} onChange={(e) => setCapital(e.target.value)} />
          </Field>
          <Field
            label="再平衡周期"
            hint="每 N 根 K 线恢复目标权重；0 表示不再平衡"
          >
            <input
              className="input num"
              type="number"
              min={0}
              value={rebalance}
              onChange={(e) => setRebalance(e.target.value)}
            />
          </Field>
          <Field label="已选" hint={`最多 ${MAX_SYMBOLS} 个，需同一市场`}>
            <div className="row" style={{ gap: 8, height: 36, flexWrap: "wrap" }}>
              <Badge tone={selected.length >= 2 ? "green" : "amber"}>{selected.length} 个</Badge>
              {selected.length > 0 ? (
                <button className="button small ghost" onClick={() => setSelected([])}>
                  清空
                </button>
              ) : null}
            </div>
          </Field>
        </div>

        {Object.entries(meta?.params_schema.properties ?? {}).length > 0 ? (
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", marginBottom: 14 }}>
            {Object.entries(meta?.params_schema.properties ?? {}).map(([key, schema]) => (
              <Field key={key} label={schema.title ?? key}>
                {schema.type === "boolean" ? (
                  <label className="row" style={{ gap: 8, height: 36 }}>
                    <input
                      type="checkbox"
                      checked={Boolean(params[key])}
                      onChange={(e) => setParams((prev) => ({ ...prev, [key]: e.target.checked }))}
                    />
                    <span className="helper">启用</span>
                  </label>
                ) : (
                  <input
                    className="input num"
                    type="number"
                    value={String(params[key] ?? "")}
                    onChange={(e) => setParams((prev) => ({ ...prev, [key]: e.target.value }))}
                  />
                )}
              </Field>
            ))}
          </div>
        ) : null}

        <div className="table-wrap" style={{ maxHeight: 340 }}>
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 34 }} />
                <th>标的</th>
                <th>市场</th>
                <th className="numeric">权重</th>
                <th className="numeric">分配资金</th>
              </tr>
            </thead>
            <tbody>
              {available.map((item) => {
                const active = selected.includes(item.code);
                const rawWeight = Number(weights[item.code]);
                const weightValid = Number.isFinite(rawWeight) && rawWeight > 0;
                const share = active
                  ? weightValid
                    ? rawWeight / selected.reduce((sum, code) => {
                        const w = Number(weights[code]);
                        return sum + (Number.isFinite(w) && w > 0 ? w : 1);
                      }, 0)
                    : 1 / selected.length
                  : 0;
                return (
                  <tr
                    key={item.code}
                    style={{ cursor: "pointer", background: active ? "var(--surface-active)" : undefined }}
                    onClick={() => toggle(item.code)}
                  >
                    <td>
                      <input type="checkbox" checked={active} readOnly />
                    </td>
                    <td>
                      <div style={{ fontWeight: 600 }}>{item.code}</div>
                      <div className="muted" style={{ fontSize: 11 }}>{item.name}</div>
                    </td>
                    <td className="muted">{item.market === "cn_stock" ? "A股" : "加密"}</td>
                    <td className="numeric">
                      <input
                        className="input num"
                        type="number"
                        min={0}
                        step={0.5}
                        placeholder="1"
                        disabled={!active}
                        value={weights[item.code] ?? ""}
                        onClick={(event) => event.stopPropagation()}
                        onChange={(event) =>
                          setWeights((prev) => ({ ...prev, [item.code]: event.target.value }))
                        }
                        style={{ width: 76, height: 30, textAlign: "right" }}
                      />
                    </td>
                    <td className="numeric mono">
                      {active ? cash(share * Number(capital || 0), currencyOf(item.market), 0) : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {run.error ? <div className="error-text" style={{ marginTop: 10 }}>{(run.error as Error).message}</div> : null}
      </Card>

      {report ? (
        <MultiResult report={report} equityOption={equityOption} />
      ) : (
        <Card>
          <EmptyState
            title="选择至少两个标的"
            text="同一策略会在每个标的上独立回测，再按资金权重合并成一条曲线。"
          />
        </Card>
      )}
    </div>
  );
}

function currencyOf(market: "crypto" | "cn_stock") {
  return market === "cn_stock" ? "CNY" : "USDT";
}

function MultiResult({
  report,
  equityOption,
}: {
  report: MultiBacktestReport;
  equityOption: ReturnType<typeof backtestEquityOption> | null;
}) {
  const metrics = report.metrics;
  const positive = metrics.total_return_pct >= 0;
  const beatBenchmark = metrics.total_return_pct > (metrics.buy_hold_pct ?? 0);
  return (
    <>
      {report.warnings.length > 0 ? (
        <div
          className="stack"
          style={{
            gap: 6,
            padding: "12px 16px",
            border: "1px solid var(--amber-border)",
            background: "var(--amber-surface)",
            borderRadius: 12,
          }}
        >
          {report.warnings.map((warning) => (
            <div key={warning} style={{ fontSize: 12.5 }}>{warning}</div>
          ))}
        </div>
      ) : null}

      <div className="grid grid-4">
        <Stat
          label="组合收益"
          value={pct(metrics.total_return_pct)}
          accent={positive ? "positive" : "negative"}
          meta={<span className="muted">{report.aligned_bars} 根共同 K 线</span>}
        />
        <Stat
          label="等权买入持有"
          value={pct(metrics.buy_hold_pct ?? 0)}
          accent={beatBenchmark ? "neutral" : "negative"}
          meta={<span className="muted">同期同篮子</span>}
        />
        <Stat
          label="最大回撤"
          value={mag(metrics.max_drawdown_pct)}
          meta={<span className="muted">持续 {metrics.max_drawdown_duration_bars} 根</span>}
        />
        <Stat
          label="交易总数"
          value={String(metrics.trade_count ?? 0)}
          meta={<span className="muted">各标的合计</span>}
        />
      </div>

      <Card
        title="组合权益曲线"
        subtitle={
          `${report.strategy} · ${report.aligned_bars} 根对齐区间 · 虚线为等权买入持有` +
          (report.rebalance_bars > 0
            ? ` · 每 ${report.rebalance_bars} 根再平衡（共 ${report.rebalance_count} 次）`
            : " · 不再平衡")
        }
      >
        {equityOption ? <Chart option={equityOption} height={300} /> : null}
      </Card>

      <Card title="逐标的表现" subtitle="名额资金按权重分配，各自独立交易" padded={false}>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>标的</th>
                <th className="numeric">权重</th>
                <th className="numeric">资金</th>
                <th className="numeric">收益</th>
                <th className="numeric">交易</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {report.components.map((item) => (
                <tr key={item.symbol}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{item.symbol}</div>
                    <div className="muted" style={{ fontSize: 11 }}>{item.name}</div>
                  </td>
                  <td className="numeric">{(item.share * 100).toFixed(1)}%</td>
                  <td className="numeric mono">{cash(item.allocation, report.currency, 0)}</td>
                  <td className={`numeric ${item.return_pct >= 0 ? "up" : "down"}`}>
                    {pct(item.return_pct)}
                  </td>
                  <td className="numeric">{item.trades}</td>
                  <td className="muted" style={{ fontSize: 11 }}>
                    {blockedHint(item.blocked)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}

function blockedHint(blocked: Record<string, number>): string {
  const parts: string[] = [];
  if (blocked.limit_up) parts.push(`涨停 ${blocked.limit_up}`);
  if (blocked.limit_down) parts.push(`跌停 ${blocked.limit_down}`);
  if (blocked.t_plus_one) parts.push(`T+1 ${blocked.t_plus_one}`);
  if (blocked.insufficient_capital) parts.push("资金不足一手");
  return parts.length ? parts.join(" · ") : "—";
}
