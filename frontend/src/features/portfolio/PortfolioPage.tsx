import { useMemo } from "react";
import ReactECharts from "echarts-for-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../shared/api";
import { useAccount } from "../../shared/account-context";
import { useBookOverview, useEquityHistory, usePositions } from "../../shared/hooks";
import { allocationOption, equityOption } from "../../shared/charts";
import { dateTimeLabel, money, pct, price, qty, signed } from "../../shared/format";
import { Card, EmptyState, Loader, Stat } from "../../components/ui";

export function PortfolioPage() {
  const { accountId } = useAccount();
  const overview = useBookOverview();
  const history = useEquityHistory(600);
  const positions = usePositions();
  const realized = useQuery({
    queryKey: ["fills-realized", accountId],
    queryFn: () => api.fills(accountId as string, 200),
    enabled: Boolean(accountId),
  });

  const points = useMemo(() => (history.data ?? []).map((p) => ({ time: p.time, equity: p.equity })), [history.data]);
  const data = overview.data;

  const drawdown = useMemo(() => {
    if (!points.length) return { max: 0, series: [] as { time: string; value: number }[] };
    let peak = points[0].equity;
    let max = 0;
    const series = points.map((point) => {
      peak = Math.max(peak, point.equity);
      const value = peak ? ((point.equity - peak) / peak) * 100 : 0;
      max = Math.min(max, value);
      return { time: point.time, value };
    });
    return { max, series };
  }, [points]);

  if (!accountId) return <EmptyState title="请先创建账户" />;

  const drawdownOption = {
    animationDuration: 300,
    grid: { left: 8, right: 14, top: 18, bottom: 4, containLabel: true },
    tooltip: { trigger: "axis", valueFormatter: (value: unknown) => `${Number(value).toFixed(2)}%` },
    xAxis: {
      type: "time",
      axisLine: { lineStyle: { color: "#e7eaf0" } },
      axisTick: { show: false },
      axisLabel: { color: "#8a94a6", fontSize: 11, hideOverlap: true },
    },
    yAxis: {
      type: "value",
      max: 0,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: "#8a94a6", fontSize: 11, formatter: "{value}%" },
      splitLine: { lineStyle: { color: "#f0f2f6" } },
    },
    series: [
      {
        type: "line",
        showSymbol: false,
        lineStyle: { color: "#e04f5f", width: 1.6 },
        areaStyle: { color: "rgba(224,79,95,0.12)" },
        data: drawdown.series.map((point) => [point.time, point.value]),
      },
    ],
  };

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid grid-4">
        <Stat label="账户权益" value={data ? `$${money(data.equity)}` : "—"} meta={<span className="muted">{data?.account_name}</span>} />
        <Stat
          label="累计收益"
          value={data ? pct(data.total_return_pct) : "—"}
          accent={(data?.total_return_pct ?? 0) >= 0 ? "positive" : "negative"}
          meta={<span className="muted">净投入 ${money(data?.net_contributions)}</span>}
        />
        <Stat
          label="最大回撤"
          value={`${Math.abs(drawdown.max).toFixed(2)}%`}
          accent="negative"
          meta={<span className="muted">基于权益快照</span>}
        />
        <Stat label="现金占比" value={data ? `${(100 - data.exposure_pct).toFixed(1)}%` : "—"} meta={<span className="muted">持仓 ${money(data?.positions_value)}</span>} />
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 2fr) minmax(300px, 1fr)" }}>
        <Card
          title="权益与回撤"
          subtitle="快照每 10 秒最多记录一次"
          padded={false}
          actions={<span className="helper">{data ? dateTimeLabel(data.updated_at) : ""}</span>}
        >
          <div style={{ padding: "6px 8px 8px" }}>
            {points.length > 1 ? (
              <>
                <ReactECharts option={equityOption(points, data?.net_contributions)} style={{ height: 260 }} notMerge />
                <ReactECharts option={drawdownOption} style={{ height: 150 }} notMerge />
              </>
            ) : (
              <EmptyState title="正在积累快照" text="产生成交后会记录权益点；也可以等待系统定时快照。" />
            )}
          </div>
        </Card>

        <Card title="配置构成" padded={false}>
          <div style={{ padding: 10 }}>
            {data ? <ReactECharts option={allocationOption(data.allocations, data.cash)} style={{ height: 250 }} notMerge /> : <Loader />}
          </div>
          <div style={{ padding: "0 18px 18px" }}>
            {data?.allocations.map((item) => (
              <div key={item.symbol} className="row" style={{ justifyContent: "space-between", fontSize: 12.5, padding: "6px 0" }}>
                <span>{item.symbol}</span>
                <span className="row" style={{ gap: 12 }}>
                  <span className="num muted">{item.weight_pct.toFixed(1)}%</span>
                  <span className={`num ${item.unrealized_pnl >= 0 ? "up" : "down"}`}>{signed(item.unrealized_pnl)}</span>
                </span>
              </div>
            ))}
          </div>
        </Card>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1.6fr) minmax(0, 1fr)" }}>
        <Card title="持仓明细" subtitle="按市值排序" padded={false}>
          {positions.data && positions.data.length > 0 ? (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>标的</th>
                    <th className="numeric">数量</th>
                    <th className="numeric">成本价</th>
                    <th className="numeric">现价</th>
                    <th className="numeric">市值</th>
                    <th className="numeric">浮盈</th>
                    <th className="numeric">浮盈率</th>
                  </tr>
                </thead>
                <tbody>
                  {[...(positions.data ?? [])]
                    .sort((a, b) => b.market_value - a.market_value)
                    .map((item) => {
                      const cost = item.avg_price * item.quantity;
                      const rate = cost ? (item.unrealized_pnl / cost) * 100 : 0;
                      return (
                        <tr key={item.symbol}>
                          <td style={{ fontWeight: 600 }}>{item.symbol}</td>
                          <td className="numeric">{qty(item.quantity)}</td>
                          <td className="numeric">{price(item.avg_price)}</td>
                          <td className="numeric">{price(item.market_price)}</td>
                          <td className="numeric">{money(item.market_value)}</td>
                          <td className={`numeric ${item.unrealized_pnl >= 0 ? "up" : "down"}`}>{signed(item.unrealized_pnl)}</td>
                          <td className={`numeric ${rate >= 0 ? "up" : "down"}`}>{pct(rate)}</td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="没有持仓" text="下单后组合构成与敞口会在这里更新。" />
          )}
        </Card>

        <Card title="最近成交" subtitle="用于核对成本" padded={false}>
          {realized.data && realized.data.length > 0 ? (
            <div className="table-wrap" style={{ maxHeight: 320 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>标的</th>
                    <th>方向</th>
                    <th className="numeric">数量</th>
                    <th className="numeric">价格</th>
                  </tr>
                </thead>
                <tbody>
                  {realized.data.slice(0, 20).map((fill) => (
                    <tr key={fill.id}>
                      <td className="mono muted">{dateTimeLabel(fill.created_at)}</td>
                      <td>{fill.symbol}</td>
                      <td className={fill.side === "buy" ? "up" : "down"}>{fill.side === "buy" ? "买入" : "卖出"}</td>
                      <td className="numeric">{qty(fill.quantity)}</td>
                      <td className="numeric">{price(fill.price)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="暂无成交" />
          )}
        </Card>
      </div>
    </div>
  );
}
