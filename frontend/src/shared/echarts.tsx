/**
 * ECharts is ~1.2MB when imported whole. Registering only the chart types and
 * components this app renders keeps the vendor chunk roughly a third of that.
 */
import * as echarts from "echarts/core";
import { BarChart, CandlestickChart, LineChart, PieChart, ScatterChart } from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import ReactEChartsCore from "echarts-for-react/lib/core";
import type { EChartsOption } from "echarts";

echarts.use([
  LineChart,
  BarChart,
  CandlestickChart,
  PieChart,
  ScatterChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  MarkLineComponent,
  CanvasRenderer,
]);

export type { EChartsOption };

export function Chart({
  option,
  height,
  notMerge = true,
  lazyUpdate = false,
}: {
  option: EChartsOption | null;
  height: number;
  notMerge?: boolean;
  lazyUpdate?: boolean;
}) {
  return (
    <ReactEChartsCore
      echarts={echarts}
      option={option ?? {}}
      style={{ height }}
      notMerge={notMerge}
      lazyUpdate={lazyUpdate}
    />
  );
}
