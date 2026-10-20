import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/api";
import { useAccount } from "../../shared/account-context";
import { useBookOverview, useFills, useOrders, usePositions, useSnapshots } from "../../shared/hooks";
import { useToast } from "../../shared/toast";
import { money, orderStatusLabel, orderStatusTone, price, qty, signed, timeOnly } from "../../shared/format";
import { Badge, Card, EmptyState, Field, Loader, Segmented, Stat } from "../../components/ui";
import { ButtonField } from "../../components/form";
import { CnTradingPanel } from "../cn/CnTradingPanel";

export function TradingPage() {
  const { account, accountId } = useAccount();
  const toast = useToast();
  const queryClient = useQueryClient();
  const overview = useBookOverview();
  const positions = usePositions();
  const openOrders = useOrders("open", 100);
  const recentOrders = useOrders(undefined, 50);
  const fills = useFills(50);
  const snapshots = useSnapshots();
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [orderType, setOrderType] = useState<"market" | "limit">("market");
  const [quantity, setQuantity] = useState("0.01");
  const [limitPrice, setLimitPrice] = useState("");

  const snapshot = snapshots.data?.find((item) => item.symbol === symbol);
  const position = positions.data?.find((item) => item.symbol === symbol);

  useEffect(() => {
    if (snapshot && !limitPrice) setLimitPrice(snapshot.price.toFixed(2));
  }, [snapshot, limitPrice]);

  const estimatedNotional = useMemo(() => {
    const unit = orderType === "limit" ? Number(limitPrice) : snapshot?.price ?? 0;
    return Number(quantity) * unit;
  }, [orderType, limitPrice, quantity, snapshot]);

  const place = useMutation({
    mutationFn: () =>
      api.placeOrder({
        account_id: accountId as string,
        symbol,
        side,
        order_type: orderType,
        quantity: Number(quantity),
        limit_price: orderType === "limit" ? Number(limitPrice) : undefined,
      }),
    onSuccess: (order) => {
      toast.push(
        order.status === "filled"
          ? `${symbol} ${side === "buy" ? "买入" : "卖出"} ${order.filled_quantity} 已成交`
          : `委托已提交，状态：${orderStatusLabel(order.status)}`,
      );
      queryClient.invalidateQueries();
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const cancel = useMutation({
    mutationFn: (orderId: string) => api.cancelOrder(orderId, accountId as string),
    onSuccess: () => {
      toast.push("订单已撤销");
      queryClient.invalidateQueries();
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const closePosition = useMutation({
    mutationFn: (target: string) => api.closePosition(accountId as string, target),
    onSuccess: () => {
      toast.push("已提交平仓市价单");
      queryClient.invalidateQueries();
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  if (!account) {
    return <EmptyState title="请先创建账户" text="交易需要一个模拟账户作为资金与持仓的载体。" />;
  }
  if (account.market === "cn_stock") {
    return <CnTradingPanel />;
  }

  const data = overview.data;
  const invalidQuantity = !Number.isFinite(Number(quantity)) || Number(quantity) <= 0;
  const invalidLimit = orderType === "limit" && (!Number.isFinite(Number(limitPrice)) || Number(limitPrice) <= 0);

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid grid-4">
        <Stat label="账户权益" value={data ? `$${money(data.equity)}` : "—"} meta={<span className="muted">{account.name}</span>} />
        <Stat label="可用现金" value={data ? `$${money(data.cash)}` : "—"} meta={<span className="muted">可用于新开仓</span>} />
        <Stat
          label="浮动盈亏"
          value={data ? `${data.unrealized_pnl >= 0 ? "+" : ""}$${money(data.unrealized_pnl)}` : "—"}
          accent={(data?.unrealized_pnl ?? 0) >= 0 ? "positive" : "negative"}
          meta={<span className="muted">{data?.position_count ?? 0} 个持仓</span>}
        />
        <Stat
          label="当前敞口"
          value={data ? `${data.exposure_pct.toFixed(1)}%` : "—"}
          meta={<span className="muted">持仓市值 ${money(data?.positions_value)}</span>}
        />
      </div>

      <div className="buy-sell-grid">
        <Card title="下单" subtitle="模拟撮合 · 含手续费与滑点">
          <div className="stack" style={{ gap: 14 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <Segmented
                value={side}
                options={[
                  { value: "buy", label: "买入" },
                  { value: "sell", label: "卖出" },
                ]}
                onChange={(value) => setSide(value)}
              />
              <Segmented
                value={orderType}
                options={[
                  { value: "market", label: "市价" },
                  { value: "limit", label: "限价" },
                ]}
                onChange={(value) => setOrderType(value)}
              />
            </div>

            <Field label="交易标的">
              <select className="select" value={symbol} onChange={(event) => {
                setSymbol(event.target.value);
                setLimitPrice("");
              }}>
                {(snapshots.data ?? []).map((item) => (
                  <option key={item.symbol} value={item.symbol}>
                    {item.symbol} · {price(item.price)}
                  </option>
                ))}
              </select>
            </Field>

            <Field label="数量" hint={position ? `当前持有 ${qty(position.quantity)}` : "当前无持仓"}>
              <input
                className="input num"
                type="number"
                min={0}
                step="0.001"
                value={quantity}
                onChange={(event) => setQuantity(event.target.value)}
              />
            </Field>

            {orderType === "limit" ? (
              <Field label="限价" hint="限价单会挂起，行情穿越后自动成交">
                <input
                  className="input num"
                  type="number"
                  min={0}
                  step="0.01"
                  value={limitPrice}
                  onChange={(event) => setLimitPrice(event.target.value)}
                />
              </Field>
            ) : null}

            <div className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
              <span className="muted">预估名义金额</span>
              <span className="num" style={{ fontWeight: 600 }}>
                ${money(estimatedNotional)}
              </span>
            </div>
            <div className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
              <span className="muted">预计手续费 (0.10%)</span>
              <span className="num">${money(estimatedNotional * 0.001)}</span>
            </div>

            <ButtonField
              variant={side === "buy" ? "primary" : "danger"}
              disabled={place.isPending || invalidQuantity || invalidLimit || !snapshot}
              onClick={() => place.mutate()}
              style={{ height: 40, fontSize: 14 }}
            >
              {place.isPending
                ? "提交中…"
                : `${side === "buy" ? "买入" : "卖出"} ${symbol.replace("USDT", "")}`}
            </ButtonField>
            <div className="helper">
              市价单按最新价加 3bp 滑点撮合；卖出仅有持仓时可通过风控。
            </div>
          </div>
        </Card>

        <div className="stack" style={{ gap: 16 }}>
          <Card title="持仓" subtitle="按市价标记" padded={false} actions={<Badge tone="indigo">{positions.data?.length ?? 0} 个</Badge>}>
            {positions.isLoading ? (
              <Loader />
            ) : positions.data && positions.data.length > 0 ? (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>标的</th>
                      <th className="numeric">数量</th>
                      <th className="numeric">均价</th>
                      <th className="numeric">现价</th>
                      <th className="numeric">市值</th>
                      <th className="numeric">浮动盈亏</th>
                      <th className="numeric">已实现</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {positions.data.map((item) => (
                      <tr key={item.symbol}>
                        <td style={{ fontWeight: 600 }}>{item.symbol}</td>
                        <td className="numeric">{qty(item.quantity)}</td>
                        <td className="numeric">{price(item.avg_price)}</td>
                        <td className="numeric">{price(item.market_price)}</td>
                        <td className="numeric">{money(item.market_value)}</td>
                        <td className={`numeric ${item.unrealized_pnl >= 0 ? "up" : "down"}`}>{signed(item.unrealized_pnl)}</td>
                        <td className="numeric muted">{signed(item.realized_pnl)}</td>
                        <td>
                          <button
                            className="button small danger"
                            disabled={closePosition.isPending}
                            onClick={() => closePosition.mutate(item.symbol)}
                          >
                            平仓
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="暂无持仓" text="左侧下单后，持仓会实时出现在这里。" />
            )}
          </Card>

          <Card title="当前委托" subtitle="未成交的限价单" padded={false}>
            {openOrders.data && openOrders.data.length > 0 ? (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>标的</th>
                      <th>方向</th>
                      <th className="numeric">数量</th>
                      <th className="numeric">限价</th>
                      <th>状态</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {openOrders.data.map((order) => (
                      <tr key={order.id}>
                        <td className="mono muted">{timeOnly(order.created_at)}</td>
                        <td>{order.symbol}</td>
                        <td>
                          <Badge tone={order.side === "buy" ? "green" : "red"}>{order.side === "buy" ? "买入" : "卖出"}</Badge>
                        </td>
                        <td className="numeric">{order.quantity}</td>
                        <td className="numeric">{order.limit_price ? price(order.limit_price) : "—"}</td>
                        <td>
                          <Badge tone={orderStatusTone(order.status)}>{orderStatusLabel(order.status)}</Badge>
                        </td>
                        <td>
                          <button className="button small" onClick={() => cancel.mutate(order.id)} disabled={cancel.isPending}>
                            撤销
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="没有挂单" text="限价单未立即成交时会进入这里，由撮合引擎在行情穿越后成交。" />
            )}
          </Card>
        </div>
      </div>

      <div className="grid grid-2">
        <Card title="订单历史" padded={false}>
          {recentOrders.data && recentOrders.data.length > 0 ? (
            <div className="table-wrap" style={{ maxHeight: 360 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>标的</th>
                    <th>类型</th>
                    <th>方向</th>
                    <th className="numeric">数量</th>
                    <th className="numeric">均价</th>
                    <th className="numeric">手续费</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {recentOrders.data.map((order) => (
                    <tr key={order.id}>
                      <td className="mono muted">{timeOnly(order.created_at)}</td>
                      <td>{order.symbol}</td>
                      <td className="muted">{order.order_type === "market" ? "市价" : "限价"}</td>
                      <td>
                        <Badge tone={order.side === "buy" ? "green" : "red"}>{order.side === "buy" ? "买" : "卖"}</Badge>
                      </td>
                      <td className="numeric">{order.quantity}</td>
                      <td className="numeric">{order.avg_fill_price ? price(order.avg_fill_price) : "—"}</td>
                      <td className="numeric muted">{money(order.fee, 4)}</td>
                      <td>
                        <Badge tone={orderStatusTone(order.status)}>{orderStatusLabel(order.status)}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="暂无订单" />
          )}
        </Card>

        <Card title="成交明细" padded={false}>
          {fills.data && fills.data.length > 0 ? (
            <div className="table-wrap" style={{ maxHeight: 360 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>标的</th>
                    <th>方向</th>
                    <th className="numeric">数量</th>
                    <th className="numeric">成交价</th>
                    <th className="numeric">名义</th>
                    <th className="numeric">手续费</th>
                  </tr>
                </thead>
                <tbody>
                  {fills.data.map((fill) => (
                    <tr key={fill.id}>
                      <td className="mono muted">{timeOnly(fill.created_at)}</td>
                      <td>{fill.symbol}</td>
                      <td>
                        <Badge tone={fill.side === "buy" ? "green" : "red"}>{fill.side === "buy" ? "买" : "卖"}</Badge>
                      </td>
                      <td className="numeric">{qty(fill.quantity)}</td>
                      <td className="numeric">{price(fill.price)}</td>
                      <td className="numeric">{money(fill.quantity * fill.price)}</td>
                      <td className="numeric muted">{money(fill.fee, 4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="暂无成交" text="成交后资金流水和持仓会同步更新。" />
          )}
        </Card>
      </div>
    </div>
  );
}
