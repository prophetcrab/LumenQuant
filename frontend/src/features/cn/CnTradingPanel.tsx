import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/api";
import type { CnSymbol } from "../../shared/api";
import { useAccount } from "../../shared/account-context";
import { useToast } from "../../shared/toast";
import {
  cashSigned,
  dateTimeLabel,
  money,
  orderStatusLabel,
  orderStatusTone,
  pct,
  price,
  qty,
  timeLabel,
} from "../../shared/format";
import { Badge, Card, EmptyState, Field, Loader, Stat } from "../../components/ui";
import { ButtonField } from "../../components/form";

export function CnTradingPanel() {
  const { account, accountId } = useAccount();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [orderType, setOrderType] = useState<"market" | "limit">("limit");
  const [quantity, setQuantity] = useState("100");
  const [limitPrice, setLimitPrice] = useState("");

  const rules = useQuery({ queryKey: ["cn-rules"], queryFn: api.cnRules, staleTime: 120_000 });
  const session = useQuery({ queryKey: ["cn-session"], queryFn: api.cnSession, refetchInterval: 10_000 });
  const symbols = useQuery({ queryKey: ["cn-symbols"], queryFn: api.cnSymbols, refetchInterval: 8_000 });
  const positions = useQuery({
    queryKey: ["positions", accountId],
    queryFn: () => api.positions(accountId as string),
    enabled: Boolean(accountId),
    refetchInterval: 5_000,
  });
  const orders = useQuery({
    queryKey: ["orders", accountId, "all"],
    queryFn: () => api.orders(accountId as string, undefined, 60),
    enabled: Boolean(accountId),
    refetchInterval: 6_000,
  });
  const thsStatus = useQuery({ queryKey: ["ths-health"], queryFn: api.thsHealth, retry: false });
  const reconcile = useQuery({
    queryKey: ["ths-reconcile", accountId],
    queryFn: () => api.thsReconcile(accountId as string),
    enabled: Boolean(accountId) && account?.kind === "live",
    retry: false,
  });

  const list: CnSymbol[] = symbols.data ?? [];
  const active = useMemo(
    () => list.find((item) => item.code === symbol) ?? list[0],
    [list, symbol],
  );
  const position = positions.data?.find((item) => item.symbol === active?.code);

  const place = useMutation({
    mutationFn: () =>
      api.placeOrder({
        account_id: accountId as string,
        symbol: (active as CnSymbol).code,
        side,
        order_type: orderType,
        quantity: Number(quantity),
        limit_price: orderType === "limit" ? Number(limitPrice) : undefined,
      }),
    onSuccess: (order) => {
      toast.push(
        order.status === "filled"
          ? `成交 ${order.filled_quantity} 股 @ ${order.avg_fill_price}`
          : `委托已提交（${order.status}），实盘成交由网关回报`,
      );
      queryClient.invalidateQueries();
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const settle = useMutation({
    mutationFn: api.settleT1,
    onSuccess: (result) => {
      toast.push(`T+1 结算完成，解锁 ${result.unlocked} 个持仓`);
      queryClient.invalidateQueries();
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  if (!account) return <EmptyState title="请先创建账户" />;
  if (account.market !== "cn_stock") {
    return (
      <EmptyState
        title="当前账户为加密市场"
        text="切换到 A 股账户，或在账户页新建一个「A股」模拟账户后使用本面板。"
      />
    );
  }

  const buyBlocked = Boolean(
    active &&
      (active.price >= active.limit_up - 0.005 || active.suspended),
  );
  const sellBlocked = Boolean(
    active &&
      (active.price <= active.limit_down + 0.005 || active.suspended),
  );
  const available = position?.available_quantity ?? 0;
  const oddLot = Number(quantity) > 0 && Number(quantity) % 100 !== 0;

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid grid-4">
        <Stat
          label="交易时段"
          value={session.data?.open ? "开市" : "休市"}
          accent={session.data?.open ? "positive" : "neutral"}
          meta={
            <span className="muted">
              {session.data?.open
                ? `${session.data.session === "morning" ? "上午" : "下午"}盘`
                : `下一开盘 ${session.data ? timeLabel(session.data.next_open) : "—"}`}
            </span>
          }
        />
        <Stat label="A股标的" value={String(list.length)} meta={<span className="muted">含主板/创业板/科创板</span>} />
        <Stat
          label="买入单位"
          value={`${rules.data?.lot_size ?? 100} 股`}
          meta={<span className="muted">T+1：当日买入次日可卖</span>}
        />
        <Stat
          label="同花顺网关"
          value={thsStatus.data?.ok ? "就绪" : thsStatus.isError ? "未配置" : "—"}
          accent={thsStatus.data?.ok ? "positive" : "neutral"}
          meta={<span className="muted">{thsStatus.data?.gateway ?? "下单通道"}</span>}
        />
      </div>

      <div className="buy-sell-grid">
        <Card title="A股下单" subtitle="涨跌停、手数、T+1 由服务端强制校验">
          <div className="stack" style={{ gap: 14 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div className="segmented">
                <button className={side === "buy" ? "active" : ""} onClick={() => setSide("buy")}>
                  买入
                </button>
                <button className={side === "sell" ? "active" : ""} onClick={() => setSide("sell")}>
                  卖出
                </button>
              </div>
              <div className="segmented">
                <button className={orderType === "limit" ? "active" : ""} onClick={() => setOrderType("limit")}>
                  限价
                </button>
                <button className={orderType === "market" ? "active" : ""} onClick={() => setOrderType("market")}>
                  市价
                </button>
              </div>
            </div>

            <Field label="股票">
              <select
                className="select"
                value={active?.code ?? ""}
                onChange={(event) => {
                  setSymbol(event.target.value);
                  setLimitPrice("");
                }}
              >
                {list.map((item) => (
                  <option key={item.code} value={item.code}>
                    {item.code} {item.name} · {price(item.price)}
                  </option>
                ))}
              </select>
            </Field>

            {active ? (
              <div
                style={{
                  padding: "10px 12px",
                  borderRadius: 10,
                  background: "var(--surface-subtle)",
                  border: "1px solid var(--border)",
                  fontSize: 12.5,
                }}
              >
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="muted">昨收 / 涨跌</span>
                  <span className="num">
                    {price(active.prev_close)} ·{" "}
                    <span className={active.change_pct >= 0 ? "up" : "down"}>{pct(active.change_pct)}</span>
                  </span>
                </div>
                <div className="row" style={{ justifyContent: "space-between", marginTop: 4 }}>
                  <span className="muted">涨停 / 跌停</span>
                  <span className="num">
                    <span className="up">{price(active.limit_up)}</span>
                    {" / "}
                    <span className="down">{price(active.limit_down)}</span>
                  </span>
                </div>
                <div className="row" style={{ justifyContent: "space-between", marginTop: 4 }}>
                  <span className="muted">板块 / 状态</span>
                  <span>
                    <Badge tone="indigo">{boardLabel(active.board)}</Badge>{" "}
                    {active.suspended ? <Badge tone="red">停牌</Badge> : null}
                    {buyBlocked ? <Badge tone="red">涨停</Badge> : null}
                    {sellBlocked ? <Badge tone="amber">跌停</Badge> : null}
                  </span>
                </div>
              </div>
            ) : null}

            <Field
              label="数量（股）"
              hint={position ? `持仓 ${qty(position.quantity)} · 可卖 ${qty(available)}` : "买入需为 100 股整数倍"}
              error={oddLot && side === "buy" ? "买入数量必须为 100 股的整数倍" : undefined}
            >
              <input
                className="input num"
                type="number"
                min={100}
                step={100}
                value={quantity}
                onChange={(event) => setQuantity(event.target.value)}
              />
            </Field>

            {orderType === "limit" ? (
              <Field label="委托价" hint={active ? `区间 ${price(active.limit_down)} – ${price(active.limit_up)}` : ""}>
                <input
                  className="input num"
                  type="number"
                  step="0.01"
                  value={limitPrice}
                  placeholder={active ? price(active.price) : ""}
                  onChange={(event) => setLimitPrice(event.target.value)}
                />
              </Field>
            ) : null}

            <ButtonField
              variant={side === "buy" ? "primary" : "danger"}
              disabled={
                place.isPending ||
                !active ||
                Number(quantity) <= 0 ||
                (orderType === "limit" && Number(limitPrice) <= 0) ||
                (side === "buy" && (buyBlocked || oddLot)) ||
                (side === "sell" && (sellBlocked || Number(quantity) > available))
              }
              onClick={() => place.mutate()}
              style={{ height: 40 }}
            >
              {place.isPending ? "提交中…" : `${side === "buy" ? "买入" : "卖出"} ${active?.name ?? ""}`}
            </ButtonField>
            <div className="helper">
              佣金 {rules.data?.fees.commission_bps ?? 2.5}bp（最低 {money(rules.data?.fees.min_commission ?? 5, 0)} 元）
              · 卖出印花税 {rules.data?.fees.stamp_duty_bps ?? 5}bp · 过户费{" "}
              {rules.data?.fees.transfer_fee_bps ?? 0.1}bp
            </div>
            {account.kind === "live" ? (
              <div className="helper" style={{ color: "var(--warning)" }}>
                实盘：委托将发往同花顺网关，成交由回报器轮询回写。
              </div>
            ) : null}
          </div>
        </Card>

        <div className="stack" style={{ gap: 16 }}>
          <Card
            title="持仓（可用/冻结）"
            padded={false}
            actions={
              <ButtonField size="small" variant="default" onClick={() => settle.mutate()} disabled={settle.isPending}>
                T+1 结算
              </ButtonField>
            }
          >
            {positions.isLoading ? (
              <Loader />
            ) : positions.data && positions.data.length > 0 ? (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>股票</th>
                      <th className="numeric">持仓</th>
                      <th className="numeric">可用</th>
                      <th className="numeric">成本</th>
                      <th className="numeric">现价</th>
                      <th className="numeric">浮盈</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.data.map((item) => (
                      <tr key={item.symbol}>
                        <td style={{ fontWeight: 600 }}>{item.symbol}</td>
                        <td className="numeric">{qty(item.quantity)}</td>
                        <td className="numeric">
                          {qty(item.available_quantity)}
                          {item.available_quantity < item.quantity ? (
                            <span className="muted"> (冻结 {qty(item.quantity - item.available_quantity)})</span>
                          ) : null}
                        </td>
                        <td className="numeric">{price(item.avg_price)}</td>
                        <td className="numeric">{price(item.market_price)}</td>
                        <td className={`numeric ${item.unrealized_pnl >= 0 ? "up" : "down"}`}>
                          {cashSigned(item.unrealized_pnl, account.base_currency)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="暂无持仓" text="买入后当日不可卖，次日开盘前自动结算解锁。" />
            )}
          </Card>

          {account.kind === "live" ? (
            <Card title="实盘对账" subtitle="本地持仓 vs 网关持仓" padded={false}>
              {reconcile.isError ? (
                <EmptyState title="对账不可用" text={(reconcile.error as Error).message} />
              ) : reconcile.data ? (
                <div style={{ padding: 14 }}>
                  <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
                    <span className="muted" style={{ fontSize: 12.5 }}>
                      网关 {reconcile.data.gateway}
                    </span>
                    <Badge tone={reconcile.data.all_match ? "green" : "red"}>
                      {reconcile.data.all_match ? "全部一致" : "存在差异"}
                    </Badge>
                  </div>
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>股票</th>
                          <th className="numeric">本地</th>
                          <th className="numeric">网关</th>
                          <th>状态</th>
                        </tr>
                      </thead>
                      <tbody>
                        {reconcile.data.positions.map((row) => (
                          <tr key={row.symbol}>
                            <td>{row.symbol}</td>
                            <td className="numeric">{qty(row.local_quantity)}</td>
                            <td className="numeric">{qty(row.gateway_quantity)}</td>
                            <td>
                              <Badge tone={row.match ? "green" : "red"}>{row.match ? "一致" : "差异"}</Badge>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ) : (
                <Loader />
              )}
            </Card>
          ) : null}
        </div>
      </div>

      <Card title="委托记录" subtitle="含网关单号与状态" padded={false}>
        {orders.data && orders.data.length > 0 ? (
          <div className="table-wrap" style={{ maxHeight: 320 }}>
            <table className="table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>股票</th>
                  <th>方向</th>
                  <th className="numeric">数量</th>
                  <th className="numeric">成交价</th>
                  <th className="numeric">费用</th>
                  <th>状态</th>
                  <th>网关单号</th>
                </tr>
              </thead>
              <tbody>
                {orders.data.map((order) => (
                  <tr key={order.id}>
                    <td className="mono muted">{dateTimeLabel(order.created_at)}</td>
                    <td>{order.symbol}</td>
                    <td>
                      <Badge tone={order.side === "buy" ? "green" : "red"}>
                        {order.side === "buy" ? "买入" : "卖出"}
                      </Badge>
                    </td>
                    <td className="numeric">{qty(order.quantity)}</td>
                    <td className="numeric">{order.avg_fill_price ? price(order.avg_fill_price) : "—"}</td>
                    <td className="numeric muted">{money(order.fee, 2)}</td>
                    <td>
                      <Badge tone={orderStatusTone(order.status)}>{orderStatusLabel(order.status)}</Badge>
                    </td>
                    <td className="mono muted">{order.broker_order_id || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="暂无委托" />
        )}
      </Card>
    </div>
  );
}

function boardLabel(board: string): string {
  const map: Record<string, string> = {
    main: "主板 ±10%",
    gem: "创业板 ±20%",
    star: "科创板 ±20%",
    bse: "北交所 ±30%",
  };
  return map[board] ?? board;
}
