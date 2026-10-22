import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/api";
import type { Alert } from "../../shared/api";
import { useAccount } from "../../shared/account-context";
import { useToast } from "../../shared/toast";
import { dateTimeLabel } from "../../shared/format";
import { Badge, Card, EmptyState, Loader, Segmented, Stat } from "../../components/ui";
import { ButtonField } from "../../components/form";

const LEVEL_LABEL: Record<string, string> = {
  info: "提示",
  warning: "警告",
  critical: "严重",
};

const LEVEL_TONE: Record<string, "red" | "amber" | "green"> = {
  critical: "red",
  warning: "amber",
  info: "green",
};

const CATEGORY_LABEL: Record<string, string> = {
  breaker: "风控熔断",
  stop_loss: "止损",
  trailing_stop: "移动止损",
  take_profit: "止盈",
  exit_failed: "保护性离场失败",
  order_rejected: "下单被拒",
  gateway_failure: "网关故障",
  data_feed: "行情中断",
};

export function AlertsPage() {
  const { account, accountId } = useAccount();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<"open" | "all">("open");
  const [category, setCategory] = useState("");
  // "" means no bound. Values are ISO strings so they map straight to the query.
  const [range, setRange] = useState<"all" | "24h" | "7d" | "30d">("all");
  const since = useMemo(() => {
    if (range === "all") return undefined;
    const hours = range === "24h" ? 24 : range === "7d" ? 24 * 7 : 24 * 30;
    return new Date(Date.now() - hours * 3600_000).toISOString();
  }, [range]);

  const categories = useQuery({
    queryKey: ["alert-categories", accountId],
    queryFn: () => api.alertCategories(accountId),
    enabled: Boolean(accountId),
    refetchInterval: 30_000,
  });
  const alerts = useQuery({
    queryKey: ["alerts", accountId, filter, category, range],
    queryFn: () =>
      api.alerts(accountId, filter === "open", 200, undefined, category || undefined, {
        since,
      }),
    enabled: Boolean(accountId),
    refetchInterval: 10_000,
  });
  const summary = useQuery({
    queryKey: ["alert-summary", accountId, range],
    queryFn: () => api.alertSummary(accountId, { since }),
    enabled: Boolean(accountId),
    refetchInterval: 10_000,
  });
  // The badge above answers "anything wrong here?"; this one answers "anything
  // wrong anywhere?", which is what an operator actually needs when a different
  // account's breaker has tripped.
  const allSummary = useQuery({
    queryKey: ["alert-summary-all", range],
    queryFn: () => api.alertSummary(undefined, { since, allAccounts: true }),
    refetchInterval: 30_000,
  });

  const ack = useMutation({
    mutationFn: (id: string) => api.ackAlert(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["alert-summary"] });
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const ackAll = useMutation({
    mutationFn: () => api.ackAllAlerts(accountId),
    onSuccess: (result) => {
      toast.push(`已确认 ${result.acknowledged} 条告警`);
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["alert-summary"] });
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  if (!account) {
    return <EmptyState title="请先创建账户" text="告警按账户记录，需要先有一个账户。" />;
  }

  const counts = summary.data?.by_level ?? {};

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid grid-4">
        <Stat
          label="待确认"
          value={String(summary.data?.unacknowledged ?? 0)}
          accent={(summary.data?.unacknowledged ?? 0) > 0 ? "negative" : "positive"}
          meta={<span className="muted">需要你判断是否处理</span>}
        />
        <Stat
          label="严重"
          value={String(counts.critical ?? 0)}
          accent={(counts.critical ?? 0) > 0 ? "negative" : "neutral"}
          meta={<span className="muted">熔断、离场失败</span>}
        />
        <Stat
          label="警告"
          value={String(counts.warning ?? 0)}
          accent={(counts.warning ?? 0) > 0 ? "negative" : "neutral"}
          meta={<span className="muted">下单被拒等</span>}
        />
        <Stat
          label="提示"
          value={String(counts.info ?? 0)}
          meta={<span className="muted">无需处理</span>}
        />
        <Stat
          label="其他账户待确认"
          value={String(
            Math.max(
              0,
              (allSummary.data?.unacknowledged ?? 0) - (summary.data?.unacknowledged ?? 0),
            ),
          )}
          accent={
            (allSummary.data?.unacknowledged ?? 0) > (summary.data?.unacknowledged ?? 0)
              ? "negative"
              : "neutral"
          }
          meta={
            <span className="muted">
              全局 {allSummary.data?.unacknowledged ?? 0} 条，含无账户归属的系统告警
            </span>
          }
        />
      </div>

      <Card
        title="告警"
        subtitle="风控熔断、保护性离场、下单失败会自动记录在这里"
        actions={
          <>
            <Segmented
              value={filter}
              options={[
                { value: "open", label: "待确认" },
                { value: "all", label: "全部" },
              ]}
              onChange={(value) => setFilter(value)}
            />
            <select
              className="select"
              style={{ width: 110, height: 30 }}
              value={range}
              onChange={(event) => setRange(event.target.value as typeof range)}
              aria-label="时间范围"
            >
              <option value="all">全部时间</option>
              <option value="24h">近 24 小时</option>
              <option value="7d">近 7 天</option>
              <option value="30d">近 30 天</option>
            </select>
            <select
              className="select"
              style={{ width: 150, height: 30 }}
              value={category}
              onChange={(event) => setCategory(event.target.value)}
            >
              <option value="">全部类型</option>
              {(categories.data ?? []).map((item) => (
                <option key={item.category} value={item.category}>
                  {CATEGORY_LABEL[item.category] ?? item.category}（{item.count}）
                </option>
              ))}
            </select>
            <ButtonField
              variant="default"
              size="small"
              disabled={ackAll.isPending || (summary.data?.unacknowledged ?? 0) === 0}
              onClick={() => ackAll.mutate()}
            >
              {ackAll.isPending ? "处理中…" : "全部确认"}
            </ButtonField>
          </>
        }
        padded={false}
      >
        {alerts.isLoading ? (
          <Loader />
        ) : alerts.data && alerts.data.length > 0 ? (
          <div className="table-wrap" style={{ maxHeight: 560 }}>
            <table className="table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>级别</th>
                  <th>类型</th>
                  <th>说明</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {alerts.data.map((item) => (
                  <AlertRow
                    key={item.id}
                    alert={item}
                    onAck={() => ack.mutate(item.id)}
                    pending={ack.isPending}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title={filter === "open" ? "没有待确认的告警" : "还没有告警"}
            text={
              filter === "open"
                ? "风控熔断、止损触发或下单失败时会出现在这里。"
                : "运行一段时间后，重要事件会记录在这里。"
            }
          />
        )}
      </Card>
    </div>
  );
}

function AlertRow({
  alert,
  onAck,
  pending,
}: {
  alert: Alert;
  onAck: () => void;
  pending: boolean;
}) {
  return (
    <tr style={{ opacity: alert.acknowledged ? 0.55 : 1 }}>
      <td className="mono muted" style={{ whiteSpace: "nowrap" }}>
        {dateTimeLabel(alert.created_at)}
      </td>
      <td>
        <Badge tone={LEVEL_TONE[alert.level] ?? ""}>{LEVEL_LABEL[alert.level] ?? alert.level}</Badge>
      </td>
      <td style={{ fontWeight: 550 }}>{CATEGORY_LABEL[alert.category] ?? alert.category}</td>
      <td>
        <div>{alert.title}</div>
        {alert.message ? (
          <div className="muted" style={{ fontSize: 11.5, marginTop: 2, lineHeight: 1.6 }}>
            {alert.message}
          </div>
        ) : null}
      </td>
      <td>
        {alert.acknowledged ? (
          <span className="helper">已确认</span>
        ) : (
          <ButtonField size="small" variant="default" disabled={pending} onClick={onAck}>
            确认
          </ButtonField>
        )}
      </td>
    </tr>
  );
}
