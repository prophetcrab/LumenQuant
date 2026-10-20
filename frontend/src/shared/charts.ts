import type { EChartsOption } from "echarts";
import type { Candle } from "./api";
import { timeLabel } from "./format";

const AXIS_COLOR = "#e7eaf0";
const LABEL_COLOR = "#8a94a6";
const TEXT_COLOR = "#4a5568";

export const palette = {
  accent: "#4f46e5",
  accentSoft: "rgba(79, 70, 229, 0.12)",
  positive: "#0e9f74",
  negative: "#e04f5f",
  amber: "#e8a33d",
  slate: "#94a3b8",
  teal: "#0f9b8e",
};


/** Round an axis bound outward to a readable step, then pad it. */
function niceBounds(min: number, max: number): { min: number; max: number } {
  const span = max - min;
  const pad = span > 0 ? span * 0.08 : Math.max(Math.abs(max) * 0.002, 1);
  const lo = min - pad;
  const hi = max + pad;
  const rough = (hi - lo) / 5;
  if (rough <= 0) return { min: lo, max: hi };
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= rough) ?? rough;
  return { min: Math.floor(lo / step) * step, max: Math.ceil(hi / step) * step };
}

export function baseTooltip(): EChartsOption["tooltip"] {
  return {
    trigger: "axis",
    backgroundColor: "rgba(255,255,255,0.97)",
    borderColor: AXIS_COLOR,
    borderWidth: 1,
    textStyle: { color: TEXT_COLOR, fontSize: 12 },
    padding: [8, 12],
    extraCssText: "border-radius:10px;box-shadow:0 10px 30px rgba(18,24,38,0.12);",
    axisPointer: { type: "line", lineStyle: { color: "#c7cdd8", width: 1 } },
  };
}

export function timeAxis(dates: string[]): Record<string, unknown> {
  return {
    type: "time",
    axisLine: { lineStyle: { color: AXIS_COLOR } },
    axisTick: { show: false },
    axisLabel: { color: LABEL_COLOR, fontSize: 11, hideOverlap: true },
    splitLine: { show: false },
    data: dates,
  };
}

export function equityOption(points: { time: string; equity: number }[], baseline?: number): EChartsOption {
  const dates = points.map((p) => p.time);
  return {
    animationDuration: 400,
    grid: { left: 8, right: 14, top: 18, bottom: 4, containLabel: true },
    tooltip: {
      ...baseTooltip(),
      valueFormatter: (value) => `$${Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 })}`,
    },
    xAxis: timeAxis(dates),
    yAxis: {
      type: "value",
      scale: true,
      // Padded and rounded outward, so the curve never hugs the border and the
      // tick labels stay short.
      min: (extent: { min: number; max: number }) => niceBounds(extent.min, extent.max).min,
      max: (extent: { min: number; max: number }) => niceBounds(extent.min, extent.max).max,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: LABEL_COLOR, fontSize: 11 },
      splitLine: { lineStyle: { color: "#f0f2f6" } },
    },
    series: [
      {
        name: "权益",
        type: "line",
        showSymbol: false,
        smooth: 0.25,
        lineStyle: { color: palette.accent, width: 2 },
        itemStyle: { color: palette.accent },
        areaStyle: {
          color: {
            type: "linear",
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: "rgba(79,70,229,0.22)" },
              { offset: 1, color: "rgba(79,70,229,0.01)" },
            ],
          },
        },
        data: points.map((p) => [p.time, p.equity]),
        markLine: baseline
          ? {
              silent: true,
              symbol: "none",
              lineStyle: { color: "#c7cdd8", type: "dashed", width: 1 },
              label: { formatter: "净投入", color: LABEL_COLOR, fontSize: 10, position: "insideEndTop" },
              data: [{ yAxis: baseline }],
            }
          : undefined,
      },
    ],
  };
}

export function allocationOption(items: { symbol: string; market_value: number }[], cash: number): EChartsOption {
  const data = [
    ...items.map((item) => ({ name: item.symbol.replace("USDT", ""), value: Number(item.market_value.toFixed(2)) })),
    ...(cash > 0.01 ? [{ name: "现金", value: Number(cash.toFixed(2)) }] : []),
  ];
  return {
    // Polling re-renders this chart every few seconds; a restarting animation
    // would leave the ring half-drawn in screenshots and feel jittery live.
    animation: false,
    tooltip: {
      ...baseTooltip(),
      trigger: "item",
      formatter: (params: unknown) => {
        const item = params as { name: string; value: number; percent: number };
        return `${item.name}<br/><b>$${item.value.toLocaleString("en-US", { maximumFractionDigits: 0 })}</b> · ${item.percent}%`;
      },
    },
    color: [palette.accent, palette.teal, palette.amber, "#8b7cf6", "#5aa9e6", "#d6b16b", palette.slate],
    series: [
      {
        type: "pie",
        radius: ["64%", "84%"],
        center: ["50%", "50%"],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: "#fff", borderWidth: 2, borderRadius: 4 },
        label: { show: false },
        emphasis: { scale: true, scaleSize: 4 },
        data,
      },
    ],
  };
}

export function candleOption(candles: Candle[], overlays: Record<string, (number | null)[]> = {}): EChartsOption {
  const times = candles.map((c) => c.time);
  const overlayColors = [palette.accent, "#e8a33d", "#0f9b8e", "#8b7cf6"];
  const overlaySeries = Object.entries(overlays).map(([name, values], index) => ({
    name,
    type: "line" as const,
    showSymbol: false,
    smooth: true,
    lineStyle: { width: 1.4, color: overlayColors[index % overlayColors.length] },
    itemStyle: { color: overlayColors[index % overlayColors.length] },
    // Category axis: values must be plain numbers aligned to `times`.
    data: values,
    xAxisIndex: 0,
    yAxisIndex: 0,
    tooltip: { show: false },
  }));

  return {
    animation: false,
    grid: [
      { left: 8, right: 12, top: 16, height: "64%", containLabel: true },
      { left: 8, right: 12, top: "76%", height: "16%", containLabel: true },
    ],
    tooltip: {
      ...baseTooltip(),
      axisPointer: { type: "cross", crossStyle: { color: "#c7cdd8" } },
    },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    xAxis: [
      {
        type: "category",
        data: times,
        boundaryGap: true,
        axisLine: { lineStyle: { color: AXIS_COLOR } },
        axisTick: { show: false },
        axisLabel: { show: false },
        splitLine: { show: false },
      },
      {
        type: "category",
        gridIndex: 1,
        data: times,
        boundaryGap: true,
        axisLine: { lineStyle: { color: AXIS_COLOR } },
        axisTick: { show: false },
        axisLabel: {
          color: LABEL_COLOR,
          fontSize: 10.5,
          hideOverlap: true,
          formatter: (value: string) => timeLabel(Number(value)),
        },
        splitLine: { show: false },
      },
    ],
    yAxis: [
      {
        scale: true,
        position: "right",
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: LABEL_COLOR, fontSize: 11 },
        splitLine: { lineStyle: { color: "#f0f2f6" } },
      },
      {
        gridIndex: 1,
        scale: true,
        position: "right",
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { show: false },
        splitLine: { show: false },
      },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], start: 55, end: 100 },
    ],
    series: [
      {
        name: "K线",
        type: "candlestick",
        data: candles.map((c) => [c.open, c.close, c.low, c.high]),
        itemStyle: {
          color: palette.positive,
          color0: palette.negative,
          borderColor: palette.positive,
          borderColor0: palette.negative,
        },
      },
      ...overlaySeries,
      {
        name: "成交量",
        type: "bar",
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: candles.map((c) => c.volume),
        itemStyle: { color: "rgba(79,70,229,0.18)", borderRadius: [2, 2, 0, 0] },
      },
    ],
  };
}

export function backtestEquityOption(points: { time: number; equity: number }[]): EChartsOption {
  const dates = points.map((p) => new Date(p.time).toISOString());
  return {
    animationDuration: 400,
    grid: { left: 8, right: 14, top: 18, bottom: 4, containLabel: true },
    tooltip: {
      ...baseTooltip(),
      valueFormatter: (value) => `$${Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 })}`,
    },
    xAxis: timeAxis(dates),
    yAxis: {
      type: "value",
      scale: true,
      // Padded and rounded outward, so the curve never hugs the border and the
      // tick labels stay short.
      min: (extent: { min: number; max: number }) => niceBounds(extent.min, extent.max).min,
      max: (extent: { min: number; max: number }) => niceBounds(extent.min, extent.max).max,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: LABEL_COLOR, fontSize: 11 },
      splitLine: { lineStyle: { color: "#f0f2f6" } },
    },
    series: [
      {
        name: "权益",
        type: "line",
        showSymbol: false,
        smooth: 0.2,
        lineStyle: { color: palette.teal, width: 2 },
        itemStyle: { color: palette.teal },
        areaStyle: {
          color: {
            type: "linear",
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: "rgba(15,155,142,0.18)" },
              { offset: 1, color: "rgba(15,155,142,0.01)" },
            ],
          },
        },
        data: points.map((p, i) => [dates[i], p.equity]),
      },
    ],
  };
}
