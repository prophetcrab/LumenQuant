import { useMemo, useState } from "react";
import { Chart } from "../../shared/echarts";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useAccount } from "../../shared/account-context";
import { useBookOverview, useEquityHistory, useOrders, useSnapshots, useSymbols } from "../../shared/hooks";
import { api } from "../../shared/api";
import { allocationOption, equityOption } from "../../shared/charts";
import { compact, dateTimeLabel, money, orderStatusLabel, orderStatusTone, pct, price, signed, timeOnly } from "../../shared/format";
import { Badge, Card, EmptyState, ErrorState, Loader, Stat } from "../../components/ui";
import { CreateAccountModal } from "../accounts/CreateAccountModal";

export function DashboardPage() {
  const { account, loading, error } = useAccount();
  const [createOpen, setCreateOpen] = useState(false);
  const overview = useBookOverview();
  const history = useEquityHistory(400);
  const snapshots = useSnapshots();
  const orders = useOrders(undefined, 6);
  const symbols = useSymbols();
  const backtests = useQuery({ queryKey: ["backtests", 5], queryFn: () => api.backtests(5) });

  const equityOptionData = useMemo(
    () => (history.data ?? []).map((point) => ({ time: point.time, equity: point.equity })),
    [history.data],
  );

  if (loading) return <Loader label="正在加载账户" />;
  if (error) return <ErrorState message={error.message} />;

  if (!account) {
    return (
      <>
        <Card>
          <div style={{ padding: "36px 20px", textAlign: "center", maxWidth: 560, margin: "0 auto" }}>
            <div className="brand-mark" style={{ margin: "0 auto 16px", width: 46, height: 46, fontSize: 17 }}>
              LQ
            </div>
            <h2 style={{ margin: "0 0 8px", fontSize: 22, letterSpacing: "-0.4px" }}>从第一个模拟账户开始</h2>
            <p className="secondary" style={{ margin: "0 auto 20px", maxWidth: 420, lineHeight: 1.7 }}>
              账户是资金、订单与策略运行的边界。创建后即可查看行情、手动下单、运行内置策略并回测。
            </p>
            <button className="button primary" onClick={() => setCreateOpen(true)}>
              创建模拟账户
            </button>
            <div className="helper" style={{ marginTop: 14 }}>
              实盘账户默认关闭，需在服务端显式开启并配置交易所密钥。
            </div>
          </div>
        </Card>
        <CreateAccountModal open={createOpen} onClose={() => setCreateOpen(false)} />
      </>
    );
  }

  const data = overview.data;
  const change = data?.change_pct ?? 0;
  const winning = (data?.unrealized_pnl ?? 0) >= 0;

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid grid-4">
        <Stat
          label="账户权益"
          value={data ? `$${money(data.equity)}` : "—"}
          delta={data?.total_return_pct}
          meta={<span className="muted">净投入 ${compact(data?.net_contributions)}</span>}
        />
        <Stat
          label="可用现金"
          value={data ? `$${money(data.cash)}` : "—"}
          meta={<span className="muted">占权益 {data ? (100 - data.exposure_pct).toFixed(1) : "—"}%</span>}
        />
        <Stat
          label="持仓市值"
          value={data ? `$${money(data.positions_value)}` : "—"}
          meta={<span className="muted">{data?.position_count ?? 0} 个持仓 · 敞口 {data?.exposure_pct ?? 0}%</span>}
        />
        <Stat
          label="浮动盈亏"
          value={data ? `${winning ? "+" : ""}$${money(data.unrealized_pnl)}` : "—"}
          accent={winning ? "positive" : "negative"}
          meta={<span className="muted">已实现 {signed(data?.realized_pnl ?? 0)}</span>}
        />
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 2.1fr) minmax(300px, 1fr)" }}>
        <Card
          title="权益曲线"
          subtitle={`${account.name} · ${data ? dateTimeLabel(data.updated_at) : ""} 更新`}
          actions={
            <>
              <span className={`delta num ${change >= 0 ? "up" : "down"}`}>
                {change >= 0 ? "▲" : "▼"} {Math.abs(change).toFixed(3)}%
              </span>
              <span className="badge indigo">{account.kind === "paper" ? "模拟" : "实盘"}</span>
            </>
          }
          padded={false}
        >
          <div style={{ padding: "4px 10px 10px" }}>
            {history.isLoading ? (
              <div className="skeleton" style={{ height: 264, margin: 12 }} />
            ) : equityOptionData.length > 1 ? (
              <Chart
                option={equityOption(equityOptionData, data?.net_contributions)}
                height={272}
                notMerge
              />
            ) : (
              <EmptyState
                title="正在积累权益快照"
                text="系统按固定节奏记录权益。产生成交或等待行情推进后，曲线会在这里展开。"
              />
            )}
          </div>
        </Card>

        <Card title="资产配置" subtitle="按市值与现金拆分">
          {data && (data.positions_value > 0 || data.cash > 0) ? (
            <>
              <Chart
                option={allocationOption(data.allocations, data.cash)}
                height={196}
                notMerge
              />
              <div className="stack" style={{ gap: 6, marginTop: 4 }}>
                {data.allocations.slice(0, 4).map((item) => (
                  <div key={item.symbol} className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
                    <span className="row" style={{ gap: 7 }}>
                      <span
                        style={{
                          width: 8,
                          height: 8,
                          borderRadius: 3,
                          background: "var(--accent)",
                          opacity: 0.85,
                        }}
                      />
                      {item.symbol}
                    </span>
                    <span className="num secondary">{item.weight_pct.toFixed(1)}%</span>
                  </div>
                ))}
                <div className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
                  <span className="row" style={{ gap: 7 }}>
                    <span style={{ width: 8, height: 8, borderRadius: 3, background: "#94a3b8" }} />
                    现金
                  </span>
                  <span className="num secondary">
                    {data.equity ? ((data.cash / data.equity) * 100).toFixed(1) : "0.0"}%
                  </span>
                </div>
              </div>
            </>
          ) : (
            <EmptyState title="尚无仓位" text="在交易页下第一笔模拟订单后，这里会展示组合构成。" />
          )}
        </Card>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 2.1fr) minmax(300px, 1fr)" }}>
        <Card
          title="最近订单"
          subtitle="同步自模拟撮合引擎"
          actions={<Link to="/trading" className="button small ghost">前往交易</Link>}
          padded={false}
        >
          {orders.isLoading ? (
            <Loader />
          ) : orders.data && orders.data.length > 0 ? (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>标的</th>
                    <th>方向</th>
                    <th className="numeric">数量</th>
                    <th className="numeric">成交价</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {orders.data.map((order) => (
                    <tr key={order.id}>
                      <td className="mono muted">{timeOnly(order.created_at)}</td>
                      <td style={{ fontWeight: 550 }}>{order.symbol}</td>
                      <td>
                        <Badge tone={order.side === "buy" ? "green" : "red"}>
                          {order.side === "buy" ? "买入" : "卖出"}
                        </Badge>
                      </td>
                      <td className="numeric">{order.quantity}</td>
                      <td className="numeric">{order.avg_fill_price ? price(order.avg_fill_price) : "—"}</td>
                      <td>
                        <Badge tone={orderStatusTone(order.status)}>{orderStatusLabel(order.status)}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState
              title="还没有订单"
              text="模拟撮合不消耗真实资金，适合验证下单链路与滑点。"
              action={<Link className="button small" to="/trading">下一笔模拟单</Link>}
            />
          )}
        </Card>

        <Card title="市场快照" subtitle="24 小时涨跌" actions={<Link to="/markets" className="button small ghost">看行情</Link>} padded={false}>
          {snapshots.isLoading ? (
            <Loader />
          ) : (
            <div className="stack" style={{ gap: 0 }}>
              {(snapshots.data ?? []).map((item, index, list) => (
                <div
                  key={item.symbol}
                  className="row"
                  style={{
                    justifyContent: "space-between",
                    padding: "10px 18px",
                    borderBottom: index < list.length - 1 ? "1px solid var(--border)" : "none",
                  }}
                >
                  <div>
                    <div style={{ fontWeight: 600, fontSize: 13 }}>{item.symbol.replace("USDT", "")}</div>
                    <div className="helper">{item.symbol}</div>
                  </div>
                  <div style={{ textAlign: "right" }}>
                    <div className="num" style={{ fontWeight: 600, fontSize: 13 }}>
                      {price(item.price)}
                    </div>
                    <div className={`num ${item.change_24h_pct >= 0 ? "up" : "down"}`} style={{ fontSize: 11.5, fontWeight: 600 }}>
                      {pct(item.change_24h_pct)}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <div className="grid grid-3">
        <Card title="最近回测" subtitle="策略研究记录">
          {backtests.data && backtests.data.length > 0 ? (
            <div className="stack" style={{ gap: 10 }}>
              {backtests.data.map((run) => (
                <Link key={run.id} to="/backtest" className="row" style={{ justifyContent: "space-between" }}>
                  <div>
                    <div style={{ fontWeight: 600, fontSize: 13 }}>{run.strategy}</div>
                    <div className="helper">
                      {run.symbol} · {run.timeframe} · {run.bars} 根
                    </div>
                  </div>
                  <div style={{ textAlign: "right" }}>
                    <div className={`num ${run.metrics.total_return_pct >= 0 ? "up" : "down"}`} style={{ fontWeight: 600 }}>
                      {pct(run.metrics.total_return_pct)}
                    </div>
                    <div className="helper">回撤 {run.metrics.max_drawdown_pct.toFixed(1)}%</div>
                  </div>
                </Link>
              ))}
            </div>
          ) : (
            <EmptyState title="还没有回测" text="从策略库选择一个模板，设定参数后即可得到权益曲线与绩效指标。" />
          )}
        </Card>

        <Card title="标的信息" subtitle="可交易品种">
          {symbols.data ? (
            <div className="stack" style={{ gap: 8 }}>
              {symbols.data.slice(0, 6).map((symbol) => (
                <div key={symbol.code} className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
                  <span className="row" style={{ gap: 8 }}>
                    <Badge tone="indigo">{symbol.base}</Badge>
                    <span className="muted">{symbol.name}</span>
                  </span>
                  <span className="num" style={{ color: "var(--text-secondary)" }}>
                    {price(symbol.initial_price)}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <Loader />
          )}
        </Card>

        <Card title="账户概览" subtitle="资金与状态">
          <div className="stack" style={{ gap: 12 }}>
            <Row label="账户" value={account.name} />
            <Row label="类型" value={account.kind === "paper" ? "模拟账户" : "实盘账户"} />
            <Row label="基础货币" value={account.base_currency} />
            <Row label="杠杆" value={`${account.leverage.toFixed(0)}x`} />
            <Row label="创建时间" value={dateTimeLabel(account.created_at)} />
            <div className="divider" />
            <Link to="/accounts" className="button small" style={{ width: "100%" }}>
              管理资金
            </Link>
          </div>
        </Card>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
      <span className="muted">{label}</span>
      <span style={{ fontWeight: 550 }}>{value}</span>
    </div>
  );
}
