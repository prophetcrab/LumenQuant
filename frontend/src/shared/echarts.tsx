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
// Import the ESM build explicitly. The package's `main` is CommonJS, and Vite's
// pre-bundling turned `import X from "echarts-for-react/lib/core"` into
// `export default require_core()`, which hands back the module namespace object
// rather than the component. React then rejected it with "Element type is
// invalid ... got: object" and the whole app failed to mount (blank page).
import ReactEChartsCore from "echarts-for-react/esm/core";
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
