import { useState } from "react";
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
};

export function AlertsPage() {
  const { account, accountId } = useAccount();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<"open" | "all">("open");

  const alerts = useQuery({
    queryKey: ["alerts", accountId, filter],
    queryFn: () =>
      api.alerts({
        account_id: accountId,
        unacknowledged_only: filter === "open",
        limit: 200,
      }),
    enabled: Boolean(accountId),
    refetchInterval: 10_000,
  });
  const summary = useQuery({
    queryKey: ["alert-summary", accountId],
    queryFn: () => api.alertSummary(accountId),
    enabled: Boolean(accountId),
    refetchInterval: 10_000,
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
