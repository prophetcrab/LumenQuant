import { useQuery } from "@tanstack/react-query";
import { api } from "../../shared/api";
import { dateTimeLabel } from "../../shared/format";
import { Badge, Card, EmptyState, Loader, Stat } from "../../components/ui";

export function SystemPage() {
  const metrics = useQuery({ queryKey: ["system-metrics"], queryFn: api.systemMetrics, refetchInterval: 5_000 });
  const audit = useQuery({ queryKey: ["audit"], queryFn: () => api.audit(60), refetchInterval: 15_000 });
  const summary = useQuery({ queryKey: ["audit-summary"], queryFn: api.auditSummary, refetchInterval: 30_000 });

  const data = metrics.data;

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid grid-4">
        <Stat
          label="运行时长"
          value={data ? formatUptime(data.requests.uptime_seconds) : "—"}
          meta={<span className="muted">当前进程</span>}
        />
        <Stat
          label="累计请求"
          value={data ? data.requests.requests.toLocaleString("en-US") : "—"}
          meta={<span className="muted">自启动以来</span>}
        />
        <Stat
          label="5xx 错误"
          value={data ? String(data.requests.server_errors) : "—"}
          accent={data && data.requests.server_errors > 0 ? "negative" : "positive"}
          meta={<span className="muted">错误率 {data ? (data.requests.error_rate * 100).toFixed(2) : "—"}%</span>}
        />
        <Stat
          label="缓存命中率"
          value={data?.caches.candles ? `${(data.caches.candles.hit_rate * 100).toFixed(1)}%` : "—"}
          meta={<span className="muted">K 线缓存</span>}
        />
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1.2fr) minmax(0, 1fr)" }}>
        <Card title="最慢接口" subtitle="按最大耗时排序" padded={false}>
          {metrics.isLoading ? (
            <Loader />
          ) : data && data.requests.slowest_routes.length > 0 ? (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>接口</th>
                    <th className="numeric">次数</th>
                    <th className="numeric">平均</th>
                    <th className="numeric">最大</th>
                    <th className="numeric">5xx</th>
                  </tr>
                </thead>
                <tbody>
                  {data.requests.slowest_routes.map(([route, stats]) => (
                    <tr key={route}>
                      <td className="mono" style={{ fontSize: 11.5 }}>{route}</td>
                      <td className="numeric">{stats.count}</td>
                      <td className="numeric">{stats.avg_ms.toFixed(1)}ms</td>
                      <td className={`numeric ${stats.max_ms > 1000 ? "down" : ""}`}>{stats.max_ms.toFixed(1)}ms</td>
                      <td className="numeric">{stats.errors}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="暂无请求数据" />
          )}
        </Card>

        <Card title="后台引擎" subtitle="运行状态">
          {data ? (
            <div className="stack" style={{ gap: 10 }}>
              {Object.entries(data.engines).map(([name, alive]) => (
                <div key={name} className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
                  <span className="muted">{engineLabel(name)}</span>
                  <Badge tone={alive ? "green" : "amber"}>{alive ? "运行中" : "未启动"}</Badge>
                </div>
              ))}
              <div className="divider" />
              {Object.entries(data.caches).map(([name, stats]) => (
                <div key={name} className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
                  <span className="muted">{name === "market" ? "行情快照缓存" : "K 线缓存"}</span>
                  <span className="num">
                    {stats.entries} 项 · 命中 {(stats.hit_rate * 100).toFixed(0)}%
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <Loader />
          )}
        </Card>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr) 300px" }}>
        <Card title="审计日志" subtitle="关键操作留痕" padded={false}>
          {audit.isLoading ? (
            <Loader />
          ) : audit.data && audit.data.length > 0 ? (
            <div className="table-wrap" style={{ maxHeight: 380 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>动作</th>
                    <th>对象</th>
                    <th>详情</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.data.map((entry) => (
                    <tr key={entry.id}>
                      <td className="mono muted">{dateTimeLabel(entry.created_at)}</td>
                      <td>
                        <Badge tone={entry.action.includes("reset") ? "amber" : "indigo"}>{entry.action}</Badge>
                      </td>
                      <td className="muted">
                        {entry.entity}
                        {entry.entity_id ? ` · ${entry.entity_id.slice(0, 8)}…` : ""}
                      </td>
                      <td className="secondary" style={{ maxWidth: 360, overflow: "hidden", textOverflow: "ellipsis" }}>
                        {JSON.stringify(entry.detail)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="暂无审计记录" text="创建账户、下单、启停策略与风控重置都会写入审计日志。" />
          )}
        </Card>

        <Card title="动作统计" subtitle="按类型">
          {summary.data ? (
            <div className="stack" style={{ gap: 8 }}>
              {summary.data.map((item) => (
                <div key={item.action} className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
                  <span className="muted">{item.action}</span>
                  <span className="num" style={{ fontWeight: 600 }}>{item.count}</span>
                </div>
              ))}
            </div>
          ) : (
            <Loader />
          )}
        </Card>
      </div>
    </div>
  );
}

function formatUptime(seconds: number): string {
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m ${total % 60}s`;
}

function engineLabel(name: string): string {
  const map: Record<string, string> = {
    market: "行情模拟器",
    trading: "撮合引擎",
    runtime: "策略调度器",
    portfolio: "组合快照器",
  };
  return map[name] ?? name;
}
