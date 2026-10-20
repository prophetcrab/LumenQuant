import { useEffect, useMemo, useState } from "react";
import { Chart, type EChartsOption } from "../../shared/echarts";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../shared/api";
import type { StrategyMeta } from "../../shared/api";
import { candleOption, palette } from "../../shared/charts";
import { categoryLabel, price, timeLabel } from "../../shared/format";
import { Badge, Card, EmptyState, Field, Loader } from "../../components/ui";
import { ButtonField } from "../../components/form";

const TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"] as const;

export function StrategiesPage() {
  const catalog = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });
  const [selected, setSelected] = useState<string | null>(null);
  const [params, setParams] = useState<Record<string, string>>({});
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState<(typeof TIMEFRAMES)[number]>("1h");
  const symbols = useQuery({ queryKey: ["symbols"], queryFn: api.symbols });

  const meta = useMemo(
    () => catalog.data?.find((item) => item.key === selected) ?? null,
    [catalog.data, selected],
  );

  useEffect(() => {
    if (!meta) return;
    const initial: Record<string, string> = {};
    Object.entries(meta.params_schema.properties ?? {}).forEach(([key, schema]) => {
      initial[key] = String(schema.default ?? meta.defaults[key] ?? "");
    });
    setParams(initial);
  }, [meta]);

  const preview = useMutation({
    mutationFn: () => {
      if (!meta) throw new Error("未选择策略");
      const parsed: Record<string, number> = {};
      Object.entries(params).forEach(([key, value]) => {
        parsed[key] = Number(value);
      });
      return api.strategySignals(meta.key, { symbol, timeframe, limit: 800, params: parsed });
    },
  });

  return (
    <div className="grid" style={{ gridTemplateColumns: "400px minmax(0, 1fr)", alignItems: "start" }}>
      <div className="stack" style={{ gap: 12 }}>
        <Card title="策略库" subtitle={`${catalog.data?.length ?? 0} 个内置模板 · 点击查看`} padded={false}>
          {catalog.isLoading ? (
            <Loader />
          ) : (
            <div style={{ maxHeight: 720, overflowY: "auto" }}>
              {(catalog.data ?? []).map((item, index, list) => (
                <StrategyRow
                  key={item.key}
                  item={item}
                  active={item.key === selected}
                  last={index === list.length - 1}
                  onClick={() => setSelected(item.key)}
                />
              ))}
            </div>
          )}
        </Card>
        <Card title="扩展说明" subtitle="未来新增策略">
          <p className="helper" style={{ margin: 0, lineHeight: 1.7 }}>
            策略以插件形式注册：继承 <span className="mono">Strategy</span> 并声明参数模型即可被目录、回测与自动运行器识别，
            无需改动引擎代码。参数 Schema 会驱动右侧表单自动生成。
          </p>
        </Card>
      </div>

      <div className="stack" style={{ gap: 16 }}>
        {meta ? (
          <>
            <Card
              title={
                <div className="row" style={{ gap: 9 }}>
                  {meta.name}
                  <Badge tone="indigo">{categoryLabel(meta.category)}</Badge>
                </div>
              }
              subtitle={meta.description}
              actions={
                <Badge tone="">{meta.tags.join(" · ")}</Badge>
              }
            >
              <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))" }}>
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
                {Object.entries(meta.params_schema.properties ?? {}).map(([key, schema]) => (
                  <Field key={key} label={schema.title ?? key} hint={`${schema.minimum ?? ""}${schema.maximum ? ` – ${schema.maximum}` : ""}`}>
                    <input
                      className="input num"
                      type="number"
                      value={params[key] ?? ""}
                      onChange={(event) => setParams((prev) => ({ ...prev, [key]: event.target.value }))}
                    />
                  </Field>
                ))}
              </div>
              <div className="row" style={{ marginTop: 16, justifyContent: "space-between" }}>
                <div className="helper">参数变更即时生效，用于研究不同市场状态下的表现。</div>
                <ButtonField disabled={preview.isPending} onClick={() => preview.mutate()}>
                  {preview.isPending ? "计算中…" : "运行信号预览"}
                </ButtonField>
              </div>
              {preview.error ? <div className="error-text" style={{ marginTop: 8 }}>{(preview.error as Error).message}</div> : null}
            </Card>

            <Card
              title="信号预览"
              subtitle={
                preview.data
                  ? `${preview.data.symbol} · ${preview.data.timeframe} · ${preview.data.signals.length} 个信号`
                  : "选择参数后运行"
              }
              padded={false}
            >
              {preview.data ? (
                <div style={{ padding: "6px 8px 4px" }}>
                  <SignalChart preview={preview.data} />
                  <div className="table-wrap" style={{ maxHeight: 220 }}>
                    <table className="table">
                      <thead>
                        <tr>
                          <th>时间</th>
                          <th>动作</th>
                          <th className="numeric">价格</th>
                          <th>原因</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[...preview.data.signals].reverse().map((signal, index) => (
                          <tr key={`${signal.time}-${index}`}>
                            <td className="mono muted">{timeLabel(signal.time)}</td>
                            <td>
                              <Badge tone={signal.action === "buy" ? "green" : "red"}>
                                {signal.action === "buy" ? "买入" : "卖出"}
                              </Badge>
                            </td>
                            <td className="numeric">{price(signal.price)}</td>
                            <td className="secondary">{signal.reason}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ) : (
                <EmptyState title="尚未生成信号" text="运行预览后会显示指标叠加、买卖点与触发原因。" />
              )}
            </Card>
          </>
        ) : (
          <Card>
            <EmptyState title="选择一个策略" text="左侧列表包含趋势、均值回归、动量与突破等经典模板，全部可调参数。" />
          </Card>
        )}
      </div>
    </div>
  );
}

function StrategyRow({
  item,
  active,
  last,
  onClick,
}: {
  item: StrategyMeta;
  active: boolean;
  last: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        display: "block",
        width: "100%",
        textAlign: "left",
        padding: "13px 18px",
        background: active ? "var(--accent-soft)" : "transparent",
        border: "none",
        borderBottom: last ? "none" : "1px solid var(--border)",
      }}
    >
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span style={{ fontWeight: 600, fontSize: 13, color: active ? "var(--accent)" : undefined }}>{item.name}</span>
        <span className="badge">{categoryLabel(item.category)}</span>
      </div>
      <div className="helper" style={{ marginTop: 4, lineHeight: 1.6 }}>
        {item.description}
      </div>
    </button>
  );
}

function SignalChart({
  preview,
}: {
  preview: {
    symbol: string;
    timeframe: string;
    signals: { time: number; price: number; action: string }[];
    overlays: Record<string, (number | null)[]>;
  };
}) {
  // Cache key must include the series length: overlays are aligned by index.
  const seriesLength = Math.max(...Object.values(preview.overlays).map((values) => values.length));
  const candles = useQuery({
    queryKey: ["signal-candles", preview.symbol, preview.timeframe, seriesLength],
    queryFn: () => api.candles(preview.symbol, preview.timeframe, seriesLength),
    staleTime: 60_000,
  });

  // Only price-scaled series belong on the candle axis; oscillators (RSI/MACD/KDJ/ADX)
  // would flatten the price scale if drawn on the same y-axis.
  const PRICE_SERIES = new Set(["fast", "slow", "ema", "middle", "upper", "lower", "center", "atr"]);

  const option = useMemo<EChartsOption | null>(() => {
    if (!candles.data) return null;
    const priceOverlays = Object.fromEntries(
      Object.entries(preview.overlays).filter(([name]) => PRICE_SERIES.has(name)),
    );
    const base = candleOption(candles.data.candles, priceOverlays);
    const series = Array.isArray(base.series) ? base.series : [];
    const times = candles.data.candles.map((c) => c.time);
    const index = new Map(times.map((time, i) => [time, i]));
    return {
      ...base,
      series: [
        ...series,
        {
          name: "买入",
          type: "scatter",
          symbol: "triangle",
          symbolSize: 11,
          itemStyle: { color: palette.positive },
          data: preview.signals
            .filter((s) => s.action === "buy")
            .map((s) => [index.get(s.time) ?? 0, s.price]),
        },
        {
          name: "卖出",
          type: "scatter",
          symbol: "triangle",
          symbolRotate: 180,
          symbolSize: 11,
          itemStyle: { color: palette.negative },
          data: preview.signals
            .filter((s) => s.action === "sell")
            .map((s) => [index.get(s.time) ?? 0, s.price]),
        },
      ],
    };
  }, [candles.data, preview]);

  if (candles.isLoading || !option) return <div className="skeleton" style={{ height: 320, margin: 10 }} />;
  return <Chart option={option} height={340} notMerge lazyUpdate />;
}
