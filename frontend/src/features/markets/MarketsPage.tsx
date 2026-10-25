import { useMemo, useState } from "react";
import { Chart } from "../../shared/echarts";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../shared/api";
import { useMarketClock, useSnapshots, useSymbols } from "../../shared/hooks";
import { candleOption } from "../../shared/charts";
import { compact, pct, price, timeLabel, timeframeLabel } from "../../shared/format";
import { Badge, Card, EmptyState, Loader, Segmented } from "../../components/ui";

const TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"] as const;
type Timeframe = (typeof TIMEFRAMES)[number];

const INDICATOR_PRESETS = [
  { value: "sma:20,ema:50", label: "均线" },
  { value: "bollinger:20:2", label: "布林带" },
  { value: "none", label: "无" },
] as const;

export function MarketsPage() {
  const symbols = useSymbols("crypto");
  const snapshots = useSnapshots("crypto");
  const clock = useMarketClock();
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState<Timeframe>("1h");
  const [specs, setSpecs] = useState<string>("sma:20,ema:50");

  const candles = useQuery({
    queryKey: ["candles", symbol, timeframe],
    queryFn: () => api.candles(symbol, timeframe, 400),
    refetchInterval: 6_000,
  });

  const indicators = useQuery({
    queryKey: ["indicators", symbol, timeframe, specs],
    queryFn: () => api.indicators(symbol, timeframe, 400, specs),
    enabled: specs !== "none",
    refetchInterval: 6_000,
  });

  const cryptoSource = useQuery({
    queryKey: ["crypto-data-source"],
    queryFn: api.cryptoDataSource,
    refetchInterval: 30_000,
  });
  const cryptoQuotes = useQuery({
    queryKey: ["crypto-quotes"],
    queryFn: api.cryptoQuotes,
    refetchInterval: 5_000,
    enabled: cryptoSource.data?.mode === "realtime-poll",
  });

  const active = snapshots.data?.find((item) => item.symbol === symbol);
  const activeQuote = cryptoQuotes.data?.quotes.find((item) => item.code === symbol);
  const overlaySeries = useMemo(() => {
    if (specs === "none" || !indicators.data) return {};
    const out: Record<string, (number | null)[]> = {};
    Object.entries(indicators.data.series).forEach(([name, values]) => {
      if (name.includes("UPPER") || name.includes("LOWER")) return;
      out[name] = values;
    });
    return out;
  }, [indicators.data, specs]);

  const option = useMemo(() => {
    if (!candles.data) return null;
    return candleOption(candles.data.candles, overlaySeries);
  }, [candles.data, overlaySeries]);

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="analysis-grid" style={{ alignItems: "start" }}>
        <Card
          padded={false}
          title={
            <div className="row" style={{ gap: 10 }}>
              <span>{symbol}</span>
              {active ? (
                <span className={`num ${active.change_24h_pct >= 0 ? "up" : "down"}`} style={{ fontSize: 13 }}>
                  {price(active.price)} · {pct(active.change_24h_pct)}
                </span>
              ) : null}
              {activeQuote && !activeQuote.fresh ? (
                <Badge tone="amber">报价滞留</Badge>
              ) : null}
            </div>
          }
          subtitle={
            (cryptoSource.data?.mode === "realtime-poll" ? "Binance 实时" : "模拟行情") +
            ` · ${timeframeLabel(timeframe)} · ${
              // Label the clock by what it actually is. Calling a 60x simulated
              // timestamp "本地时间" made the chart look hours ahead of the wall
              // clock for no visible reason; with a real feed it is real time.
              clock.data?.simulated === false ? "交易所时间" : "模拟时钟"
            } ${clock.data ? timeLabel(clock.data.simulated_now) : "—"}`
          }
          actions={
            <>
              <Segmented
                value={timeframe}
                options={TIMEFRAMES.map((item) => ({ value: item, label: item }))}
                onChange={(value) => setTimeframe(value)}
              />
              <select className="select" style={{ width: 140 }} value={specs} onChange={(event) => setSpecs(event.target.value)}>
                {INDICATOR_PRESETS.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </>
          }
        >
          <div style={{ padding: "6px 8px 4px" }}>
            {candles.isLoading ? (
              <div className="skeleton" style={{ height: 656, margin: 10 }} />
            ) : option ? (
              <Chart option={option} height={656} notMerge lazyUpdate />
            ) : (
              <EmptyState title="暂无行情数据" text="行情引擎启动后会自动合成历史 K 线。" />
            )}
          </div>
        </Card>

        <Card title="标的" subtitle="点击切换" padded={false}>
          <div>
            {(snapshots.data ?? []).map((item, index, list) => {
              const selected = item.symbol === symbol;
              return (
                <button
                  key={item.symbol}
                  onClick={() => setSymbol(item.symbol)}
                  style={{
                    display: "flex",
                    width: "100%",
                    alignItems: "center",
                    justifyContent: "space-between",
                    padding: "12px 18px",
                    background: selected ? "var(--accent-soft)" : "transparent",
                    border: "none",
                    borderBottom: index < list.length - 1 ? "1px solid var(--border)" : "none",
                    textAlign: "left",
                  }}
                >
                  <div>
                    <div style={{ fontWeight: 600, fontSize: 13, color: selected ? "var(--accent)" : undefined }}>
                      {item.symbol.replace("USDT", "")}
                      <span className="muted" style={{ fontWeight: 400, marginLeft: 6, fontSize: 11.5 }}>
                        /USDT
                      </span>
                    </div>
                    <div className="helper">
                      高 {price(item.high_24h)} · 低 {price(item.low_24h)}
                    </div>
                  </div>
                  <div style={{ textAlign: "right" }}>
                    <div className="num" style={{ fontWeight: 600, fontSize: 13 }}>
                      {price(item.price)}
                    </div>
                    <div className={`num ${item.change_24h_pct >= 0 ? "up" : "down"}`} style={{ fontSize: 11.5, fontWeight: 600 }}>
                      {pct(item.change_24h_pct)}
                    </div>
                  </div>
                </button>
              );
            })}
            {snapshots.isLoading ? <Loader /> : null}
          </div>
        </Card>
      </div>

      <div className="grid grid-3">
        <Card title="24 小时统计" subtitle={symbol}>
          {active ? (
            <div className="stack" style={{ gap: 10 }}>
              <Row label="最新价" value={price(active.price)} />
              <Row label="24h 涨跌" value={pct(active.change_24h_pct)} tone={active.change_24h_pct >= 0 ? "up" : "down"} />
              <Row label="24h 最高" value={price(active.high_24h)} />
              <Row label="24h 最低" value={price(active.low_24h)} />
              {/* The backend sums bar volume, which is a base-asset quantity
                  (shares or coins), not a notional amount in quote currency.
                  Labelling it 成交额 told users the number was money. */}
              <Row
                label="24h 成交量"
                value={`${compact(active.volume_24h)} ${
                  active.market === "cn_stock" ? "股" : active.symbol.replace(/USDT$/, "")
                }`}
              />
            </div>
          ) : (
            <Loader />
          )}
        </Card>

        <Card title="指标读数" subtitle="与图表联动">
          {specs === "none" ? (
            <EmptyState title="未启用指标" text="选择“均线”或“布林带”后会显示叠加线与最新读数。" />
          ) : indicators.data ? (
            <div className="stack" style={{ gap: 10 }}>
              {Object.entries(indicators.data.series)
                .filter(([name]) => !name.includes("UPPER") && !name.includes("LOWER"))
                .slice(0, 6)
                .map(([name, values]) => {
                  const last = [...values].reverse().find((value) => value !== null);
                  return <Row key={name} label={name} value={last === undefined || last === null ? "—" : price(last)} />;
                })}
            </div>
          ) : (
            <Loader />
          )}
        </Card>

        <Card title="品种目录" subtitle="系统支持的交易标的">
          {symbols.data ? (
            <div className="stack" style={{ gap: 8 }}>
              {symbols.data.map((item) => (
                <div key={item.code} className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
                  <span className="row" style={{ gap: 8 }}>
                    <Badge tone="indigo">{item.base}</Badge>
                    <span className="muted">{item.asset_class}</span>
                  </span>
                  <span className="helper">
                    最小变动 {item.tick_size} · 步长 {item.lot_size}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <Loader />
          )}
        </Card>
      </div>
    </div>
  );
}

function Row({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  return (
    <div className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
      <span className="muted">{label}</span>
      <span className={`num ${tone ?? ""}`} style={{ fontWeight: 550 }}>
        {value}
      </span>
    </div>
  );
}
