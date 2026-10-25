import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/api";
import { useAccount } from "../../shared/account-context";
import { useToast } from "../../shared/toast";
import { money, pct } from "../../shared/format";
import { Badge, Card, Loader } from "../../components/ui";

export function RiskPanel() {
  const { accountId } = useAccount();
  const toast = useToast();
  const queryClient = useQueryClient();
  const limits = useQuery({ queryKey: ["risk-limits"], queryFn: api.riskLimits, staleTime: 60_000 });
  const state = useQuery({
    queryKey: ["risk-state", accountId],
    queryFn: () => api.riskState(accountId as string),
    enabled: Boolean(accountId),
    refetchInterval: 8_000,
  });
  const broker = useQuery({ queryKey: ["broker-status"], queryFn: api.brokerStatus, staleTime: 60_000 });

  const reset = useMutation({
    mutationFn: () => api.resetRisk(accountId as string),
    onSuccess: () => {
      toast.push("熔断已重置，日内基准重新计算");
      queryClient.invalidateQueries({ queryKey: ["risk-state"] });
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  if (!accountId) return null;
  const tripped = state.data?.breaker_active ?? false;

  return (
    <div className="grid grid-3">
      <Card title="风控状态" subtitle="账户级熔断">
        {state.isLoading || !state.data ? (
          <Loader />
        ) : (
          <div className="stack" style={{ gap: 10 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="muted" style={{ fontSize: 12.5 }}>
                交易闸门
              </span>
              <Badge tone={tripped ? "red" : "green"}>
                {tripped
                  ? state.data.halt_kind === "drawdown"
                    ? "回撤熔断（需人工重置）"
                    : "日内熔断"
                  : "正常"}
              </Badge>
            </div>
            <div className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
              <span className="muted">
                日内基准权益
                {state.data.day_key ? <span className="helper"> · {state.data.day_key}</span> : null}
              </span>
              <span className="num">{state.data.day_start_equity ? money(state.data.day_start_equity) : "—"}</span>
            </div>
            <div className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
              <span className="muted">历史峰值权益</span>
              <span className="num">{state.data.peak_equity ? money(state.data.peak_equity) : "—"}</span>
            </div>
            {tripped ? (
              <>
                <div className="error-text">{state.data.breaker_reason}</div>
                <button className="button small" onClick={() => reset.mutate()} disabled={reset.isPending}>
                  确认风险后重置
                </button>
              </>
            ) : (
              <div className="helper">
                日损失或回撤触线后自动禁止新开仓；跨交易日后日内亏损额度会自动重置，回撤峰值保留。
              </div>
            )}
          </div>
        )}
      </Card>

      <Card title="限额" subtitle="服务端配置">
        {limits.data ? (
          <div className="stack" style={{ gap: 8 }}>
            <Row label="单笔名义上限" value={money(limits.data.max_order_notional, 0)} />
            <Row label="单标的持仓上限" value={money(limits.data.max_position_notional, 0)} />
            <Row label="最小下单金额" value={money(limits.data.min_order_notional, 0)} />
            <Row label="每分钟订单数" value={String(limits.data.max_orders_per_minute)} />
            <Row label="日损失熔断" value={limits.data.max_daily_loss_pct ? pct(-limits.data.max_daily_loss_pct * 100) : "关闭"} />
            <Row label="回撤熔断" value={limits.data.max_drawdown_pct ? `${(limits.data.max_drawdown_pct * 100).toFixed(0)}%` : "关闭"} />
          </div>
        ) : (
          <Loader />
        )}
      </Card>

      <Card title="实盘通道" subtitle="默认关闭">
        {broker.data ? (
          <div className="stack" style={{ gap: 10 }}>
            <Row label="实盘开关" value={broker.data.live_trading_enabled ? "已开启" : "关闭"} />
            <Row
              label="自动下单"
              value={broker.data.live_auto_trading_enabled ? "已开启" : "关闭"}
            />
            {(
              [
                ["binance", "币安（加密）"],
                ["ths", "同花顺（A股）"],
              ] as const
            ).map(([key, label]) => {
              const channel = broker.data.channels[key];
              return (
                <div
                  key={key}
                  className="row"
                  style={{ justifyContent: "space-between", fontSize: 12.5 }}
                >
                  <span className="muted">{label}</span>
                  <Badge tone={channel?.ready ? "green" : "amber"}>
                    {channel?.ready ? "就绪" : "未就绪"}
                  </Badge>
                </div>
              );
            })}
            <Row
              label="成交回报轮询"
              value={
                broker.data.reconciler_running
                  ? `运行中 · 每 ${broker.data.reconcile_seconds}s`
                  : "未运行"
              }
            />
            <div className="helper">
              实盘需同时满足：服务端开关开启 + 环境变量提供密钥。密钥不落库、不经前端传输。
              成交回报轮询未运行时，实盘委托会一直停在「已提交」，不会变成「已成交」。
            </div>
          </div>
        ) : (
          <Loader />
        )}
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
