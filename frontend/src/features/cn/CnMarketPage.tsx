import { useQuery } from "@tanstack/react-query";
import { api } from "../../shared/api";
import { pct, price, timeLabel } from "../../shared/format";
import { Badge, Card, Loader, Stat } from "../../components/ui";

export function CnMarketPage() {
  const rules = useQuery({ queryKey: ["cn-rules"], queryFn: api.cnRules, staleTime: 120_000 });
  const session = useQuery({ queryKey: ["cn-session"], queryFn: api.cnSession, refetchInterval: 10_000 });
  const calendar = useQuery({ queryKey: ["cn-calendar"], queryFn: () => api.cnCalendar(10), staleTime: 3600_000 });
  const symbols = useQuery({ queryKey: ["cn-symbols"], queryFn: api.cnSymbols, refetchInterval: 8_000 });
  const channels = useQuery({ queryKey: ["broker-channels"], queryFn: api.brokerChannels, staleTime: 60_000 });

  const ths = channels.data?.channels.ths;

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid grid-4">
        <Stat
          label="市场状态"
          value={session.data?.open ? "交易中" : "休市"}
          accent={session.data?.open ? "positive" : "neutral"}
          meta={
            <span className="muted">
              {session.data?.open
                ? timeLabel(session.data.session_close) + " 收盘"
                : "下一开盘 " + (session.data ? timeLabel(session.data.next_open) : "—")}
            </span>
          }
        />
        <Stat label="A股标的" value={String(symbols.data?.length ?? 0)} meta={<span className="muted">沪 / 深</span>} />
        <Stat
          label="买入单位"
          value={`${rules.data?.lot_size ?? 100} 股`}
          meta={<span className="muted">T+1 当日买入次日可卖</span>}
        />
        <Stat
          label="同花顺通道"
          value={ths?.ready ? "就绪" : "未就绪"}
          accent={ths?.ready ? "positive" : "neutral"}
          meta={
            <span className="muted">
              {ths?.mock_enabled ? "演练网关" : ths?.gateway_configured ? "已配置" : "未配置"}
            </span>
          }
        />
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1.6fr) minmax(320px, 1fr)" }}>
        <Card title="A股标的" subtitle="实时价、涨跌停与板块" padded={false}>
          {symbols.isLoading ? (
            <Loader />
          ) : (
            <div className="table-wrap" style={{ maxHeight: 430 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>代码</th>
                    <th>名称</th>
                    <th>板块</th>
                    <th className="numeric">现价</th>
                    <th className="numeric">涨跌</th>
                    <th className="numeric">涨停</th>
                    <th className="numeric">跌停</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {(symbols.data ?? []).map((item) => (
                    <tr key={item.code}>
                      <td className="mono" style={{ fontWeight: 600 }}>{item.code}</td>
                      <td>{item.name}</td>
                      <td>
                        <Badge tone="indigo">{boardLabel(item.board)}</Badge>
                      </td>
                      <td className="numeric">{price(item.price)}</td>
                      <td className={`numeric ${item.change_pct >= 0 ? "up" : "down"}`}>{pct(item.change_pct)}</td>
                      <td className="numeric up">{price(item.limit_up)}</td>
                      <td className="numeric down">{price(item.limit_down)}</td>
                      <td>
                        {item.suspended ? (
                          <Badge tone="red">停牌</Badge>
                        ) : item.price >= item.limit_up - 0.005 ? (
                          <Badge tone="red">涨停</Badge>
                        ) : item.price <= item.limit_down + 0.005 ? (
                          <Badge tone="amber">跌停</Badge>
                        ) : (
                          <Badge tone="green">正常</Badge>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <div className="stack" style={{ gap: 16 }}>
          <Card title="交易规则" subtitle="由服务端强制执行">
            {rules.data ? (
              <div className="stack" style={{ gap: 8 }}>
                <Row label="买入单位" value={`${rules.data.lot_size} 股整数倍`} />
                <Row label="卖出零股" value="仅可一次性全部卖出" />
                <Row label="交收" value={rules.data.settlement} />
                <div className="divider" />
                {Object.entries(rules.data.boards).map(([name, value]) => (
                  <Row key={name} label={boardLabel(name)} value={`±${(value.limit_pct * 100).toFixed(0)}%`} />
                ))}
                <Row label="ST 股" value={`±${(rules.data.st_limit_pct * 100).toFixed(0)}%`} />
                <div className="divider" />
                <Row label="佣金" value={`${rules.data.fees.commission_bps}bp（最低 ${rules.data.fees.min_commission} 元）`} />
                <Row label="印花税" value={`${rules.data.fees.stamp_duty_bps}bp（仅卖出）`} />
                <Row label="过户费" value={`${rules.data.fees.transfer_fee_bps}bp`} />
              </div>
            ) : (
              <Loader />
            )}
          </Card>

          <Card title="交易日" subtitle="节假日与调休">
            {calendar.data ? (
              <div className="stack" style={{ gap: 8 }}>
                <Row label="今日" value={calendar.data.is_trading_day ? "交易日" : "休市"} />
                <Row label="上一交易日" value={calendar.data.prev_trading_day} />
                <Row label="下一交易日" value={calendar.data.next_trading_day} />
                <div className="divider" />
                <div className="helper">未来 {calendar.data.upcoming.length} 个交易日</div>
                <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                  {calendar.data.upcoming.map((day) => (
                    <span key={day} className="badge">{day.slice(5)}</span>
                  ))}
                </div>
              </div>
            ) : (
              <Loader />
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
      <span className="muted">{label}</span>
      <span className="num" style={{ fontWeight: 550 }}>{value}</span>
    </div>
  );
}

function boardLabel(board: string): string {
  const map: Record<string, string> = {
    main: "主板",
    gem: "创业板",
    star: "科创板",
    bse: "北交所",
  };
  return map[board] ?? board;
}
