import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../shared/api";
import { useAccount } from "../../shared/account-context";
import { useToast } from "../../shared/toast";
import { cash, cashSigned, dateTimeLabel, pct, price, qty } from "../../shared/format";
import { Badge, Card, EmptyState, Loader, Stat } from "../../components/ui";
import { ButtonField } from "../../components/form";

export function RiskPage() {
  const { accountId } = useAccount();
  const toast = useToast();
  const queryClient = useQueryClient();
  const overview = useQuery({
    queryKey: ["risk-overview", accountId],
    queryFn: () => api.riskOverview(accountId as string),
    enabled: Boolean(accountId),
    refetchInterval: 6_000,
  });
  const reset = useMutation({
    mutationFn: () => api.resetRisk(accountId as string),
    onSuccess: () => {
      toast.push("熔断已重置，日内基准重新计算");
      queryClient.invalidateQueries({ queryKey: ["risk-overview"] });
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  if (!accountId) return <EmptyState title="请先创建账户" />;
  if (overview.isLoading) return <Loader label="正在汇总风险数据" />;
  if (overview.isError) {
    return <EmptyState title="风险数据不可用" text={(overview.error as Error).message} />;
  }

  const data = overview.data!;
  const currency = data.account.base_currency;
  const state = data.state;
  const limits = data.limits;
  const exp = data.exposure;
  const unprotected = data.instances.filter((item) => item.running && !item.protected);

  return (
    <div className="stack" style={{ gap: 16 }}>
      {data.warnings.length > 0 ? (
        <div
          style={{
            padding: "12px 16px",
            borderRadius: 12,
            background: "var(--negative-soft)",
            border: "1px solid #f3c9cf",
            color: "var(--negative)",
            fontSize: 13,
            lineHeight: 1.7,
          }}
        >
          <div style={{ fontWeight: 700, marginBottom: 4 }}>需要处理</div>
          {data.warnings.map((warn) => (
            <div key={warn}>· {warn}</div>
          ))}
        </div>
      ) : null}

      <div className="grid grid-4">
        <Stat
          label="交易闸门"
          value={state.breaker_active ? "已熔断" : "正常"}
          accent={state.breaker_active ? "negative" : "positive"}
          meta={
            <span className="muted">
              {state.breaker_active
                ? state.halt_kind === "drawdown"
                  ? "回撤熔断，需人工重置"
                  : "日内熔断，跨日自动恢复"
                : "允许新开仓"}
            </span>
          }
        />
        <Stat
          label="当前敞口"
          value={`${exp.exposure_pct.toFixed(1)}%`}
          meta={<span className="muted">最大单一持仓 {exp.largest_position_pct.toFixed(1)}%</span>}
        />
        <Stat
          label="可用 / 冻结现金"
          value={cash(exp.available_cash, currency)}
          meta={<span className="muted">挂单冻结 {cash(exp.reserved_cash, currency)}</span>}
        />
        <Stat
          label="受保护实例"
          value={`${data.instances.filter((i) => i.protected).length} / ${data.instances.length}`}
          accent={unprotected.length > 0 ? "negative" : "positive"}
          meta={<span className="muted">{unprotected.length} 个运行中实例无止损</span>}
        />
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)" }}>
        <Card title="账户级熔断" subtitle={`基准日 ${state.day_key || "—"}`}>
          <div className="stack" style={{ gap: 10 }}>
            <Row
              label="日内基准权益"
              value={state.day_start_equity ? cash(state.day_start_equity, currency) : "—"}
            />
            <Row
              label="历史峰值权益"
              value={state.peak_equity ? cash(state.peak_equity, currency) : "—"}
            />
            <Row label="当前权益" value={cash(exp.equity, currency)} />
            <div className="divider" />
            <Row
              label="日损失限额"
              value={limits.max_daily_loss_pct ? `-${(limits.max_daily_loss_pct * 100).toFixed(1)}%` : "关闭"}
            />
            <Row
              label="回撤限额"
              value={limits.max_drawdown_pct ? `${(limits.max_drawdown_pct * 100).toFixed(1)}%` : "关闭"}
            />
            {state.breaker_active ? (
              <>
                <div className="error-text">{state.breaker_reason}</div>
                <ButtonField size="small" variant="default" onClick={() => reset.mutate()} disabled={reset.isPending}>
                  {reset.isPending ? "重置中…" : "确认风险后重置"}
                </ButtonField>
              </>
            ) : (
              <div className="helper">
                日损失触线跨日自动恢复；回撤触线需人工确认，避免结构性亏损被自动解除。
              </div>
            )}
            <div className="helper">更新时间 {dateTimeLabel(state.updated_at)}</div>
          </div>
        </Card>

        <Card title="下单限额" subtitle="服务端强制，无法从前端绕过">
          <div className="stack" style={{ gap: 10 }}>
            <Row label="单笔名义上限" value={cash(limits.max_order_notional, currency, 0)} />
            <Row label="单标的持仓上限" value={cash(limits.max_position_notional, currency, 0)} />
            <Row label="最小下单金额" value={cash(limits.min_order_notional, currency, 0)} />
            <Row label="每分钟订单数" value={String(limits.max_orders_per_minute)} />
            <div className="divider" />
            <div className="helper">
              自动运行实例的单次仓位与名义上限在实例上单独配置，二者同时生效。
            </div>
          </div>
        </Card>
      </div>

      <Card title="持仓风险" subtitle="按市值排序，含止损参考价" padded={false}>
        {data.positions.length > 0 ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>标的</th>
                  <th className="numeric">持仓 / 可用</th>
                  <th className="numeric">成本</th>
                  <th className="numeric">现价</th>
                  <th className="numeric">市值</th>
                  <th className="numeric">浮盈</th>
                  <th className="numeric">浮盈率</th>
                  <th className="numeric">3% 止损参考</th>
                </tr>
              </thead>
              <tbody>
                {[...data.positions]
                  .sort((a, b) => b.market_value - a.market_value)
                  .map((item) => (
                    <tr key={item.symbol}>
                      <td style={{ fontWeight: 600 }}>{item.symbol}</td>
                      <td className="numeric">
                        {qty(item.quantity)} / {qty(item.available_quantity)}
                      </td>
                      <td className="numeric">{price(item.avg_price)}</td>
                      <td className="numeric">{price(item.market_price)}</td>
                      <td className="numeric">{cash(item.market_value, currency)}</td>
                      <td className={`numeric ${item.unrealized_pnl >= 0 ? "up" : "down"}`}>
                        {cashSigned(item.unrealized_pnl, currency)}
                      </td>
                      <td className={`numeric ${item.unrealized_pct >= 0 ? "up" : "down"}`}>
                        {pct(item.unrealized_pct)}
                      </td>
                      <td className="numeric muted">{price(item.avg_price * 0.97)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="没有持仓" text="开仓后这里会显示每个标的的止损参考价位。" />
        )}
      </Card>

      <Card title="自动运行保护状态" subtitle="未设止损的运行中实例会被标红" padded={false}>
        {data.instances.length > 0 ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>策略</th>
                  <th>标的</th>
                  <th>状态</th>
                  <th className="numeric">止损</th>
                  <th className="numeric">止盈</th>
                  <th className="numeric">移动止损</th>
                  <th className="numeric">峰值</th>
                  <th className="numeric">执行</th>
                  <th>最近错误</th>
                </tr>
              </thead>
              <tbody>
                {data.instances.map((item) => (
                  <tr key={item.id}>
                    <td style={{ fontWeight: 600 }}>{item.strategy}</td>
                    <td>{item.symbol}</td>
                    <td>
                      <Badge tone={item.running ? "green" : ""}>
                        {item.running ? "运行中" : "已停止"}
                      </Badge>
                    </td>
                    <td className={`numeric ${item.stop_loss_pct ? "" : "down"}`}>
                      {item.stop_loss_pct ? `${(item.stop_loss_pct * 100).toFixed(1)}%` : "未设"}
                    </td>
                    <td className="numeric">
                      {item.take_profit_pct ? `${(item.take_profit_pct * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td className="numeric">
                      {item.trailing_stop_pct ? `${(item.trailing_stop_pct * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td className="numeric muted">
                      {item.peak_price ? price(item.peak_price) : "—"}
                    </td>
                    <td className="numeric">{item.executions}</td>
                    <td className="error-text" style={{ maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis" }}>
                      {item.last_error || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title="没有自动运行实例"
            text="在自动运行页创建实例时可设置止损、止盈与移动止损。"
          />
        )}
      </Card>

      <Card title="资金占用" subtitle="账面现金与可用现金的差异来自挂单冻结">
        <div className="grid grid-4">
          <Metric label="账面现金" value={cash(exp.cash, currency)} />
          <Metric label="挂单冻结" value={cash(exp.reserved_cash, currency)} />
          <Metric label="可用现金" value={cash(exp.available_cash, currency)} />
          <Metric label="持仓市值" value={cash(exp.positions_value, currency)} />
        </div>
      </Card>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
      <span className="muted">{label}</span>
      <span className="num" style={{ fontWeight: 550 }}>
        {value}
      </span>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ padding: "10px 12px", background: "var(--surface-subtle)", borderRadius: 10 }}>
      <div className="helper">{label}</div>
      <div className="num" style={{ fontWeight: 600, fontSize: 15, marginTop: 3 }}>
        {value}
      </div>
    </div>
  );
}
