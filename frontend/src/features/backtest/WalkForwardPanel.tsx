import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../shared/api";
import type { WalkForwardReport } from "../../shared/api";
import { useToast } from "../../shared/toast";
import { pct } from "../../shared/format";
import { Badge, Card, Field, Loader } from "../../components/ui";
import { ButtonField } from "../../components/form";

export function WalkForwardPanel({
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
      properties?: Record<
        string,
        { type?: string; title?: string; minimum?: number; maximum?: number }
      >;
    };
    defaults: Record<string, number | string | boolean>;
  } | null;
}) {
  const toast = useToast();
  const [windows, setWindows] = useState("4");
  const [bars, setBars] = useState("1200");
  const [objective, setObjective] = useState("risk_adjusted");
  const [ranges, setRanges] = useState<Record<string, string>>({});
  const [report, setReport] = useState<WalkForwardReport | null>(null);
  const seededFor = useRef("");

  const numericParams = useMemo(() => {
    const props = meta?.params_schema.properties ?? {};
    return Object.entries(props).filter(
      ([, schema]) => schema.type === "integer" || schema.type === "number",
    );
  }, [meta]);

  const booleanParams = useMemo(() => {
    const props = meta?.params_schema.properties ?? {};
    return Object.entries(props).filter(([, schema]) => schema.type === "boolean");
  }, [meta]);

  // Keep grids small: each window re-runs the whole search.
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
      const values = [0.6, 1, 1.6]
        .map((factor) => Math.round(base * factor))
        .map((value) => Math.max(min, Math.min(max, value)));
      seeded[name] = Array.from(new Set(values)).join(",");
    });
    setRanges(seeded);
  }, [strategy, meta]);

  const buildSpace = () => {
    const space: Record<string, (number | boolean)[]> = {};
    Object.entries(ranges).forEach(([key, raw]) => {
      const values = raw
        .split(",")
        .map((part) => Number(part.trim()))
        .filter((value) => Number.isFinite(value));
      if (values.length) space[key] = values;
    });
    booleanParams.forEach(([key]) => {
      space[key] = [true, false];
    });
    return space;
  };

  const run = useMutation({
    mutationFn: () => {
      const space = buildSpace();
      if (Object.keys(space).length === 0) {
        throw new Error("请为至少一个参数填写候选值");
      }
      return api.walkForwardBacktest({
        strategy,
        symbol,
        timeframe,
        bars: Number(bars),
        space,
        objective,
        windows: Number(windows),
      });
    },
    onSuccess: (result) => {
      setReport(result);
      toast.push(
        `${result.aggregate.windows_completed} 个窗口完成，样本外均值 ${pct(
          result.aggregate.mean_test_return_pct ?? 0,
        )}`,
      );
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const agg = report?.aggregate;

  return (
    <div className="stack" style={{ gap: 16 }}>
      <Card
        title="滚动验证（Walk-Forward）"
        subtitle="每个窗口独立选参，再把参数原样用到紧随其后的样本外区间"
        actions={
          <ButtonField size="small" onClick={() => run.mutate()} disabled={run.isPending}>
            {run.isPending ? "验证中…" : "开始验证"}
          </ButtonField>
        }
      >
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" }}>
          <Field label="窗口数" hint="2–12；数据不足会自动缩减">
            <input
              className="input num"
              type="number"
              min={2}
              max={12}
              value={windows}
              onChange={(e) => setWindows(e.target.value)}
            />
          </Field>
          <Field label="K 线数量" hint="至少 400">
            <input
              className="input num"
              type="number"
              min={400}
              max={5000}
              value={bars}
              onChange={(e) => setBars(e.target.value)}
            />
          </Field>
          <Field label="优化目标">
            <select className="select" value={objective} onChange={(e) => setObjective(e.target.value)}>
              <option value="risk_adjusted">风险调整</option>
              <option value="return">总收益</option>
              <option value="sharpe">Sharpe</option>
              <option value="calmar">Calmar</option>
            </select>
          </Field>
        </div>

        <div style={{ marginTop: 14 }}>
          <div className="helper" style={{ marginBottom: 8 }}>
            每个窗口的训练段都会重新搜索这组候选值——范围越大，单次验证越慢。
          </div>
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
            {numericParams.map(([key, schema]) => (
              <Field key={key} label={schema.title ?? key}>
                <input
                  className="input mono"
                  value={ranges[key] ?? ""}
                  onChange={(e) => setRanges((prev) => ({ ...prev, [key]: e.target.value }))}
                />
              </Field>
            ))}
          </div>
          {booleanParams.length > 0 ? (
            <div className="helper" style={{ marginTop: 8 }}>
              布尔参数自动测试两种取值：
              {booleanParams.map(([key, schema]) => (
                <Badge key={key} tone="indigo">
                  {schema.title ?? key}
                </Badge>
              ))}
            </div>
          ) : null}
        </div>
      </Card>

      {run.isPending ? <Loader label="正在逐窗口搜索与验证" /> : null}

      {report && agg ? (
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
              <div style={{ fontWeight: 700, marginBottom: 4 }}>稳健性提示</div>
              {report.warnings.map((warn) => (
                <div key={warn}>· {warn}</div>
              ))}
            </div>
          ) : (
            <div
              style={{
                padding: "10px 16px",
                borderRadius: 12,
                background: "var(--positive-soft)",
                border: "1px solid #c9e9dc",
                color: "var(--positive)",
                fontSize: 13,
              }}
            >
              未发现明显过拟合信号，但样本外结果仍可能受单一市场状态影响。
            </div>
          )}

          <div className="grid grid-4">
            <div className="card stat-card">
              <div className="stat-label">窗口胜率</div>
              <div className="stat-value num">{agg.positive_pct?.toFixed(0) ?? "—"}%</div>
              <div className="stat-meta">
                <span className="muted">
                  {agg.positive_windows ?? 0} / {agg.windows_completed ?? 0} 个窗口为正
                </span>
              </div>
            </div>
            <div className="card stat-card">
              <div className="stat-label">样本外均值</div>
              <div className="stat-value num">{pct(agg.mean_test_return_pct ?? 0)}</div>
              <div className="stat-meta">
                <span className="muted">最差 {pct(agg.worst_test_return_pct ?? 0)}</span>
              </div>
            </div>
            <div className="card stat-card">
              <div className="stat-label">训练→测试衰减</div>
              <div className="stat-value num">{pct(agg.mean_degradation_pct ?? 0)}</div>
              <div className="stat-meta">
                <span className="muted">越大说明训练期越被高估</span>
              </div>
            </div>
            <div className="card stat-card">
              <div className="stat-label">累计样本外收益</div>
              <div className="stat-value num">{pct(agg.compounded_test_return_pct ?? 0)}</div>
              <div className="stat-meta">
                <span className="muted">
                  耗时 {report.elapsed_ms.toFixed(0)}ms
                  {agg.skipped_windows ? ` · 跳过 ${agg.skipped_windows}` : ""}
                </span>
              </div>
            </div>
          </div>

          <Card title="逐窗口结果" subtitle="每个窗口的训练收益与紧随其后的样本外收益" padded={false}>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>窗口</th>
                    <th>训练区间</th>
                    <th>参数</th>
                    <th className="numeric">训练收益</th>
                    <th className="numeric">样本外</th>
                    <th className="numeric">衰减</th>
                    <th className="numeric">回撤</th>
                    <th className="numeric">交易</th>
                  </tr>
                </thead>
                <tbody>
                  {report.windows.map((window) => (
                    <tr key={window.index}>
                      <td className="muted">W{window.index}</td>
                      <td className="mono muted" style={{ fontSize: 11 }}>
                        {window.train_start}–{window.train_end} / {window.test_start}–{window.test_end}
                      </td>
                      <td className="mono" style={{ fontSize: 11 }}>
                        {Object.entries(window.params)
                          .map(([k, v]) =>
                            typeof v === "boolean" ? `${k}=${v ? "on" : "off"}` : `${k}=${v}`,
                          )
                          .join(" ")}
                      </td>
                      <td className="numeric">{pct(window.train_return_pct)}</td>
                      <td
                        className={`numeric ${
                          window.test_return_pct >= 0 ? "up" : "down"
                        }`}
                      >
                        {pct(window.test_return_pct)}
                      </td>
                      <td className="numeric muted">{pct(window.degradation_pct)}</td>
                      <td className="numeric">{window.test_drawdown_pct.toFixed(2)}%</td>
                      <td className="numeric">{window.test_trades}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <div className="helper">
            滚动验证比单次 70/30 切分可靠，但仍受标的选择区间影响。
            窗口胜率低于 50% 或衰减过大时，不建议据此调参。
          </div>
        </>
      ) : null}
    </div>
  );
}
