import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Chart } from "../../shared/echarts";
import { api } from "../../shared/api";
import type { BacktestRun, PortfolioReport } from "../../shared/api";
import { backtestEquityOption } from "../../shared/charts";
import { cash, pct } from "../../shared/format";
import { Badge, Card, EmptyState, Field, Stat } from "../../components/ui";
import { ButtonField } from "../../components/form";

type Props = {
  history: BacktestRun[];
};

const MAX_SLEEVES = 8;

// Portfolio runs must share a market (the backend rejects mixed ones), so the
// report's own currency field applies to every amount it shows.
const currencyOf = (report: { currency?: string; market: "crypto" | "cn_stock" }) =>
  report.currency ?? (report.market === "cn_stock" ? "CNY" : "USDT");

/** Volatility and drawdown are magnitudes, not signed changes: no leading "+". */
const mag = (value: number) => `${value.toFixed(2)}%`;

export function PortfolioPanel({ history }: Props) {
  const [selected, setSelected] = useState<string[]>([]);
  const [weights, setWeights] = useState<Record<string, string>>({});
  const [capital, setCapital] = useState("100000");
  // Basket runs already contain per-symbol curves; importing them saves the user
  // from re-running each symbol just to see their correlation.
  const baskets = useQuery({
    queryKey: ["multi-backtests", 20],
    queryFn: () => api.multiBacktests(20),
  });
  const [basketId, setBasketId] = useState("");
  // Runs saved before sleeve persistence exist without children; they would fill
  // the list with options that do nothing when chosen.
  const importable = useMemo(
    () => (baskets.data ?? []).filter((item) => (item.child_run_ids?.length ?? 0) > 0),
    [baskets.data],
  );

  // Sleeves imported from a basket are ordinary runs but hidden from the
  // single-symbol history, so they must be fetched to be shown and weighted.
  const historyIds = useMemo(
    () => new Set((history ?? []).map((item) => item.id)),
    [history],
  );
  const orphanIds = useMemo(
    () => selected.filter((id) => !historyIds.has(id)),
    [selected, historyIds],
  );
  const imported = useQuery({
    queryKey: ["imported-runs", orphanIds],
    queryFn: async () => {
      const rows = await Promise.all(orphanIds.map((id) => api.backtest(id)));
      return rows;
    },
    enabled: orphanIds.length > 0,
  });

  const runs = useMemo(() => {
    const base = (history ?? []).filter((item) => item.bars >= 50).slice(0, 60);
    const extra = (imported.data ?? []).filter(
      (item) => !base.some((existing) => existing.id === item.id),
    );
    // Imported sleeves first: they are what the user just acted on.
    return [...extra, ...base];
  }, [history, imported.data]);

  const toggle = (id: string) => {
    setSelected((prev) => {
      if (prev.includes(id)) {
        const next = prev.filter((item) => item !== id);
        setWeights((current) => {
          const copy = { ...current };
          delete copy[id];
          return copy;
        });
        return next;
      }
      if (prev.length >= MAX_SLEEVES) return prev;
      return [...prev, id];
    });
  };

  const analyse = useMutation({
    mutationFn: () => {
      const payloadWeights = selected.map((id) => {
        const raw = Number(weights[id]);
        return Number.isFinite(raw) && raw > 0 ? raw : 1;
      });
      const uniform = payloadWeights.every((value) => value === payloadWeights[0]);
      return api.portfolioBacktest({
        run_ids: selected,
        // Omit weights when the user left everything blank/uniform, so the
        // server's equal-weight path is used instead of a rounded approximation.
        weights: uniform ? undefined : payloadWeights,
        initial_capital: Number(capital),
      });
    },
  });

  const report = analyse.data ?? null;
  const equityOption = useMemo(
    () => (report ? backtestEquityOption(report.equity, undefined, currencyOf(report)) : null),
    [report],
  );

  return (
    <div className="stack" style={{ gap: 16 }}>
      <Card
        title="组合分析"
        subtitle="选择多个已保存的回测，检验它们叠加后是否真的分散风险"
        actions={
          <ButtonField
            disabled={selected.length < 2 || analyse.isPending}
            onClick={() => analyse.mutate()}
          >
            {analyse.isPending ? "计算中…" : `分析 ${selected.length} 个回测`}
          </ButtonField>
        }
      >
        {importable.length > 0 ? (
          <Field
            label="从多标的回测导入"
            hint="把一次篮子回测的各标的曲线直接加入组合"
          >
            <select
              className="select"
              value={basketId}
              onChange={(event) => {
                const id = event.target.value;
                setBasketId(id);
                const basket = baskets.data?.find((item) => item.id === id);
                const ids = basket?.child_run_ids ?? [];
                if (ids.length > 0) {
                  setSelected(ids);
                  // Restore the basket's own weights instead of resetting to
                  // equal: the user configured that mix deliberately, and the
                  // whole point of importing is to study *that* portfolio.
                  // child_run_ids is aligned with symbols/weights, so index
                  // pairing is safe.
                  const restored: Record<string, string> = {};
                  ids.forEach((runId, index) => {
                    const weight = basket?.weights?.[index];
                    if (typeof weight === "number" && weight > 0) {
                      restored[runId] = String(weight);
                    }
                  });
                  setWeights(restored);
                  setCapital(String(Math.round(basket?.initial_capital ?? 100000)));
                }
              }}
            >
              <option value="">选择一次多标的回测…</option>
              {importable.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.strategy} · {item.symbols.join("/")} · {item.timeframe}
                </option>
              ))}
            </select>
          </Field>
        ) : null}

        <div className="grid grid-2" style={{ gap: 12, marginBottom: 14 }}>
          <Field label="组合初始资金" hint="按权重分配到各回测">
            <input
              className="input num"
              type="number"
              value={capital}
              onChange={(event) => setCapital(event.target.value)}
            />
          </Field>
          <Field
            label="已选"
            hint={`最多 ${MAX_SLEEVES} 个；周期与市场必须一致`}
          >
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

        {runs.length === 0 ? (
          <EmptyState title="没有可用的回测" text="先在上方「单次回测」里跑几个策略，再回来组合。" />
        ) : (
          <div className="table-wrap" style={{ maxHeight: 340 }}>
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 34 }} />
                  <th>策略</th>
                  <th>标的</th>
                  <th>周期</th>
                  <th className="numeric">收益</th>
                  <th className="numeric">回撤</th>
                  <th className="numeric">K 线</th>
                  <th className="numeric">权重</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const active = selected.includes(run.id);
                  return (
                    <tr
                      key={run.id}
                      style={{ cursor: "pointer", background: active ? "var(--surface-active)" : undefined }}
                      onClick={() => toggle(run.id)}
                    >
                      <td>
                        <input type="checkbox" checked={active} readOnly />
                      </td>
                      <td style={{ fontWeight: 600 }}>{run.strategy}</td>
                      <td>{run.symbol}</td>
                      <td className="muted">{run.timeframe}</td>
                      <td className={`numeric ${run.metrics.total_return_pct >= 0 ? "up" : "down"}`}>
                        {pct(run.metrics.total_return_pct)}
                      </td>
                      <td className="numeric muted">{pct(run.metrics.max_drawdown_pct)}</td>
                      <td className="numeric muted">{run.bars}</td>
                      <td className="numeric">
                        <input
                          className="input num"
                          type="number"
                          min={0}
                          step={0.5}
                          placeholder="1"
                          disabled={!active}
                          value={weights[run.id] ?? ""}
                          onClick={(event) => event.stopPropagation()}
                          onChange={(event) =>
                            setWeights((prev) => ({ ...prev, [run.id]: event.target.value }))
                          }
                          style={{ width: 76, height: 30, textAlign: "right" }}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {analyse.error ? <div className="error-text" style={{ marginTop: 10 }}>{(analyse.error as Error).message}</div> : null}
      </Card>

      {report ? <PortfolioResult report={report} equityOption={equityOption} /> : null}
    </div>
  );
}

function PortfolioResult({
  report,
  equityOption,
}: {
  report: PortfolioReport;
  equityOption: ReturnType<typeof backtestEquityOption> | null;
}) {
  const positive = report.metrics.total_return_pct >= 0;
  const currency = currencyOf(report);
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
            <div key={warning} style={{ fontSize: 12.5 }}>
              {warning}
            </div>
          ))}
        </div>
      ) : null}

      <div className="grid grid-4">
        <Stat
          label="组合收益"
          value={pct(report.metrics.total_return_pct)}
          accent={positive ? "positive" : "negative"}
          meta={<span className="muted">{report.aligned_bars} 根共同 K 线</span>}
        />
        <Stat
          label="组合 Sharpe"
          value={report.metrics.sharpe.toFixed(2)}
          meta={<span className="muted">年化波动 {mag(report.metrics.volatility_pct)}</span>}
        />
        <Stat
          label="最大回撤"
          value={mag(report.metrics.max_drawdown_pct)}
          meta={<span className="muted">持续 {report.metrics.max_drawdown_duration_bars} 根</span>}
        />
        <Stat
          label="分散效果"
          value={`${report.diversification.benefit_pct.toFixed(1)}%`}
          accent={report.diversification.benefit_pct > 0 ? "positive" : "neutral"}
          meta={
            <span className="muted">
              加权 {mag(report.diversification.weighted_volatility_pct)} → 实际{" "}
              {mag(report.diversification.combined_volatility_pct)}
            </span>
          }
        />
      </div>

      <Card title="组合权益曲线" subtitle={`${report.timeframe} · ${report.aligned_bars} 根对齐区间`}>
        {equityOption ? <Chart option={equityOption} height={300} /> : null}
      </Card>

      <div className="grid grid-2" style={{ alignItems: "start" }}>
        <Card title="成分" subtitle="权重按组合资金换算为各自的名义资金" padded={false}>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>策略 / 标的</th>
                  <th className="numeric">权重</th>
                  <th className="numeric">资金</th>
                  <th className="numeric">收益</th>
                  <th className="numeric">波动</th>
                  <th className="numeric">回撤</th>
                </tr>
              </thead>
              <tbody>
                {report.components.map((item) => (
                  <tr key={item.run_id}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{item.strategy}</div>
                      <div className="muted mono" style={{ fontSize: 11 }}>
                        {item.symbol} · {item.bars_aligned}/{item.bars_total} 根
                      </div>
                    </td>
                    <td className="numeric">{(item.weight * 100).toFixed(1)}%</td>
                    <td className="numeric mono">{cash(item.allocation, currency, 0)}</td>
                    <td className={`numeric ${item.return_pct >= 0 ? "up" : "down"}`}>
                      {pct(item.return_pct)}
                    </td>
                    <td className="numeric muted">{mag(item.volatility_pct)}</td>
                    <td className="numeric muted">{mag(item.max_drawdown_pct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card title="相关性矩阵" subtitle="逐根收益率的相关；越接近 1 越是同一个风险">
          <CorrelationMatrix labels={report.correlation.labels} matrix={report.correlation.matrix} />
        </Card>
      </div>
    </>
  );
}

function CorrelationMatrix({ labels, matrix }: { labels: string[]; matrix: number[][] }) {
  // Keep the strategy and the run discriminator: dropping everything after the
  // first " · " collapsed duplicate runs into identical row headers.
  // The label is "strategy · symbol" (plus a run discriminator when two rows are
  // identical). Showing only the strategy collapsed every same-strategy basket
  // into identical headers; the symbol is what distinguishes them.
  const short = labels.map((label) => {
    const [strategy, rest] = label.split(" · ");
    if (!rest) return strategy;
    const tag = rest.split(" #")[1];
    const symbol = rest.split(" #")[0];
    return tag ? `${symbol} #${tag}` : symbol;
  });
  const cell = (value: number) => {
    if (value === 1) return { background: "var(--surface-active)", color: "var(--text-muted)" };
    // Diverging scale: green for negative (diversifying), amber/red for positive.
    const strength = Math.min(1, Math.abs(value));
    if (value < 0) {
      return { background: `rgba(16, 185, 129, ${0.10 + strength * 0.45})`, color: "var(--text)" };
    }
    return { background: `rgba(244, 114, 82, ${0.10 + strength * 0.5})`, color: "var(--text)" };
  };
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="table" style={{ tableLayout: "fixed" }}>
        <thead>
          <tr>
            <th style={{ width: 120 }} />
            {short.map((label, index) => (
              <th key={`${label}-${index}`} className="numeric" style={{ fontSize: 11 }}>
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.map((row, i) => (
            <tr key={`${short[i]}-${i}`}>
              <td style={{ fontWeight: 600, fontSize: 12 }}>{short[i]}</td>
              {row.map((value, j) => (
                <td
                  key={j}
                  className="numeric mono"
                  style={{ ...cell(value), fontSize: 12, padding: "8px 6px" }}
                >
                  {value.toFixed(2)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="helper" style={{ marginTop: 8 }}>
        绿色越深表示负相关（分散有效），红色越深表示同涨同跌。
      </div>
    </div>
  );
}
