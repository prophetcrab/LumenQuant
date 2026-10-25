import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../shared/api";
import type { OptimisationReport } from "../../shared/api";
import { useToast } from "../../shared/toast";
import { num, pct } from "../../shared/format";
import { Badge, Card, Field, Loader } from "../../components/ui";
import { ButtonField } from "../../components/form";

const OBJECTIVES = [
  { value: "risk_adjusted", label: "风险调整（收益/回撤 + Sharpe）" },
  { value: "return", label: "总收益" },
  { value: "sharpe", label: "Sharpe" },
  { value: "calmar", label: "Calmar（收益/回撤）" },
];

export function OptimisePanel({
  strategy,
  symbol,
  timeframe,
  meta,
}: {
  strategy: string;
  symbol: string;
  timeframe: string;
  meta: {
    params_schema: {
      properties?: Record<string, { type?: string; title?: string; minimum?: number; maximum?: number }>;
    };
    defaults: Record<string, number | string | boolean>;
  } | null;
}) {
  const toast = useToast();
  const [objective, setObjective] = useState("risk_adjusted");
  const [bars, setBars] = useState("800");
  const [report, setReport] = useState<OptimisationReport | null>(null);
  const [ranges, setRanges] = useState<Record<string, string>>({});
  const seededFor = useRef<string>("");

  // Only numeric integer params are griddable; booleans get a two-value grid and
  // free-form strings are skipped (they have no natural ordering).
  const numericParams = useMemo(() => {
    const props = meta?.params_schema.properties ?? {};
    return Object.entries(props).filter(
      ([, schema]) => schema.type === "integer" || schema.type === "number",
    );
  }, [meta]);

  // Seed the numeric boxes with a sensible spread around each default so the
  // panel is usable immediately: an empty grid produces a single meaningless
  // "best" combination.
  useEffect(() => {
    const key = `${strategy}:${JSON.stringify(meta?.defaults ?? {})}`;
    if (seededFor.current === key) return;
    seededFor.current = key;
    const seeded: Record<string, string> = {};
    const props = meta?.params_schema.properties ?? {};
    Object.entries(props).forEach(([name, schema]) => {
      if (schema.type !== "integer" && schema.type !== "number") return;
      const raw = meta?.defaults[name];
      const base = typeof raw === "number" ? raw : Number(schema.minimum ?? 10);
      const min = schema.minimum ?? 1;
      const max = schema.maximum ?? base * 4;
      const candidates = [0.5, 0.75, 1, 1.5, 2]
        .map((factor) => Math.round(base * factor))
        .map((value) => Math.max(min, Math.min(max, value)));
      seeded[name] = Array.from(new Set(candidates)).join(",");
    });
    setRanges(seeded);
  }, [strategy, meta]);

  const booleanParams = useMemo(() => {
    const props = meta?.params_schema.properties ?? {};
    return Object.entries(props).filter(([, schema]) => schema.type === "boolean");
  }, [meta]);

  const buildSpace = () => {
    const space: Record<string, (number | boolean)[]> = {};
    Object.entries(ranges).forEach(([key, raw]) => {
      const values = raw
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean)
        .map((part) => Number(part))
        .filter((value) => Number.isFinite(value));
      if (values.length > 0) space[key] = values;
    });
    booleanParams.forEach(([key]) => {
      space[key] = [true, false];
    });
    return space;
  };

  const spaceSize = useMemo(() => {
    const space = buildSpace();
    const keys = Object.keys(space);
    if (keys.length === 0) return 0;
    return keys.reduce((total, key) => total * space[key].length, 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ranges, booleanParams]);

  const run = useMutation({
    mutationFn: () => {
      const space = buildSpace();
      if (Object.keys(space).length === 0) {
        throw new Error("请为至少一个数字参数填写候选值，例如 5,10,15");
      }
      return api.optimiseBacktest({
        strategy,
        symbol,
        timeframe,
        bars: Number(bars),
        space,
        objective,
        config: {},
        top_n: 10,
      });
    },
    onSuccess: (result) => {
      setReport(result);
      toast.push(
        `测试 ${result.tested} 组，最优收益 ${result.best?.total_return_pct.toFixed(2) ?? "—"}%`,
      );
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const tooMany = spaceSize > 500;

  return (
    <div className="stack" style={{ gap: 16 }}>
      <Card
        title="参数优化（网格搜索）"
        subtitle="在每个参数上枚举候选值，用样本内数据选优、样本外数据检验"
        actions={
          <ButtonField
            size="small"
            onClick={() => run.mutate()}
            disabled={run.isPending || spaceSize === 0 || tooMany}
          >
            {run.isPending ? "搜索中…" : "开始搜索"}
          </ButtonField>
        }
      >
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))" }}>
          <Field label="优化目标">
            <select className="select" value={objective} onChange={(e) => setObjective(e.target.value)}>
              {OBJECTIVES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="K 线数量" hint="至少 250，需留出样本外区间">
            <input
              className="input num"
              type="number"
              min={250}
              max={5000}
              value={bars}
              onChange={(e) => setBars(e.target.value)}
            />
          </Field>
        </div>

        <div style={{ marginTop: 14 }}>
          <div className="helper" style={{ marginBottom: 8 }}>
            候选值用逗号分隔，例如 <span className="mono">5,10,15,20</span>。留空则不参与搜索。
          </div>
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
            {numericParams.map(([key, schema]) => (
              <Field
                key={key}
                label={schema.title ?? key}
                hint={`${schema.minimum ?? ""}${schema.maximum ? ` – ${schema.maximum}` : ""}`}
              >
                <input
                  className="input mono"
                  placeholder="例如 5,10,15"
                  value={ranges[key] ?? ""}
                  onChange={(e) => setRanges((prev) => ({ ...prev, [key]: e.target.value }))}
                />
              </Field>
            ))}
          </div>
          {booleanParams.length > 0 ? (
            <div className="helper" style={{ marginTop: 8 }}>
              布尔参数会自动测试 true / false 两种取值：
              {booleanParams.map(([key, schema]) => (
                <Badge key={key} tone="indigo">
                  {schema.title ?? key}
                </Badge>
              ))}
            </div>
          ) : null}
        </div>

        <div className="row" style={{ marginTop: 14, justifyContent: "space-between" }}>
          <span className={`helper ${tooMany ? "error-text" : ""}`}>
            组合数 {spaceSize}
            {tooMany ? " · 超过 500 上限，请减少候选值" : ""}
          </span>
          <span className="helper">
            样本内 {report?.in_sample_bars ?? "—"} 根 / 样本外 {report?.out_of_sample_bars ?? "—"} 根
          </span>
        </div>
      </Card>

      {run.isPending ? <Loader label="正在回测每个参数组合" /> : null}

      {report ? (
        <>
          {report.warnings.length > 0 ? (
            <div
              style={{
                padding: "12px 16px",
                borderRadius: 12,
                background: "var(--warning-soft)",
                border: "1px solid #f3ddb8",
                color: "#7a4a06",
                fontSize: 13,
                lineHeight: 1.7,
              }}
            >
              <div style={{ fontWeight: 700, marginBottom: 4 }}>
                稳健性提示{report.data_source === "synthetic" ? "（数据源非真实行情）" : ""}
              </div>
              {report.warnings.map((warn) => (
                <div key={warn}>· {warn}</div>
              ))}
            </div>
          ) : null}

          <div className="grid grid-4">
            <div className="card stat-card">
              <div className="stat-label">最优收益</div>
              <div className="stat-value num">
                {report.best ? pct(report.best.total_return_pct) : "—"}
              </div>
              <div className="stat-meta">
                <span className="muted">
                  样本外 {report.best ? pct(report.best.out_of_sample_return_pct) : "—"}
                </span>
              </div>
            </div>
            <div className="card stat-card">
              <div className="stat-label">盈利组合占比</div>
              <div className="stat-value num">
                {report.distribution.profitable_pct?.toFixed(1) ?? "—"}%
              </div>
              <div className="stat-meta">
                <span className="muted">
                  {report.distribution.profitable_count ?? 0} / {report.valid} 组
                </span>
              </div>
            </div>
            <div className="card stat-card">
              <div className="stat-label">收益离散度</div>
              <div className="stat-value num">
                {report.distribution.spread_pct?.toFixed(1) ?? "—"}%
              </div>
              <div className="stat-meta">
                <span className="muted">
                  {pct(report.distribution.worst_return_pct ?? 0)} ~{" "}
                  {pct(report.distribution.best_return_pct ?? 0)}
                </span>
              </div>
            </div>
            <div className="card stat-card">
              <div className="stat-label">搜索规模</div>
              <div className="stat-value num">{report.tested}</div>
              <div className="stat-meta">
                <span className="muted">
                  有效 {report.valid} · 耗时 {report.elapsed_ms.toFixed(0)}ms
                </span>
              </div>
            </div>
          </div>

          <Card title="参数组合排名" subtitle={`按 ${report.objective} 排序`} padded={false}>
            <div className="table-wrap" style={{ maxHeight: 420 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>参数</th>
                    <th className="numeric">收益</th>
                    <th className="numeric">回撤</th>
                    <th className="numeric">Sharpe</th>
                    <th className="numeric">交易</th>
                    <th className="numeric">胜率</th>
                    <th className="numeric">样本内</th>
                    <th className="numeric">样本外</th>
                  </tr>
                </thead>
                <tbody>
                  {report.top.map((item, index) => (
                    <tr key={index}>
                      <td className="muted">{index + 1}</td>
                      <td className="mono" style={{ fontSize: 11.5 }}>
                        {formatParams(item.params)}
                      </td>
                      <td className={`numeric ${item.total_return_pct >= 0 ? "up" : "down"}`}>
                        {pct(item.total_return_pct)}
                      </td>
                      <td className="numeric">{item.max_drawdown_pct.toFixed(2)}%</td>
                      <td className="numeric">{num(item.sharpe)}</td>
                      <td className="numeric">{item.trade_count}</td>
                      <td className="numeric">{item.win_rate_pct.toFixed(0)}%</td>
                      <td className="numeric muted">{pct(item.in_sample_return_pct)}</td>
                      <td
                        className={`numeric ${
                          item.out_of_sample_return_pct >= 0 ? "up" : "down"
                        }`}
                      >
                        {pct(item.out_of_sample_return_pct)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          {report.robust_alternatives.length > 0 ? (
            <Card
              title="参数邻域稳健候选"
              subtitle="表现接近最优、且不是孤立峰值 —— 更可能是真实优势"
              padded={false}
            >
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>参数</th>
                      <th className="numeric">收益</th>
                      <th className="numeric">回撤</th>
                      <th className="numeric">样本外</th>
                      <th className="numeric">交易</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.robust_alternatives.map((item, index) => (
                      <tr key={index}>
                        <td className="mono" style={{ fontSize: 11.5 }}>
                          {formatParams(item.params)}
                        </td>
                        <td className={`numeric ${item.total_return_pct >= 0 ? "up" : "down"}`}>
                          {pct(item.total_return_pct)}
                        </td>
                        <td className="numeric">{item.max_drawdown_pct.toFixed(2)}%</td>
                        <td className="numeric muted">{pct(item.out_of_sample_return_pct)}</td>
                        <td className="numeric">{item.trade_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          ) : null}

          <div className="helper">
            网格搜索一定能在历史数据上找到"最好"的参数——这本身不构成策略有效的证据。
            请优先看样本外表现、盈利组合占比与邻域稳健性。
          </div>
        </>
      ) : null}
    </div>
  );
}

function formatParams(params: Record<string, number | boolean | string>): string {
  return Object.entries(params)
    .map(([key, value]) =>
      typeof value === "boolean" ? `${key}=${value ? "on" : "off"}` : `${key}=${value}`,
    )
    .join(" ");
}
