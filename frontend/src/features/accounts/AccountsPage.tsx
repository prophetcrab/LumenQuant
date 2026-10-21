import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/api";
import { useAccount } from "../../shared/account-context";
import { useToast } from "../../shared/toast";
import { cash, cashSigned, dateTimeLabel } from "../../shared/format";
import { Badge, Card, EmptyState, Field, Loader, Modal } from "../../components/ui";
import { ButtonField } from "../../components/form";
import { CreateAccountModal } from "./CreateAccountModal";

export function AccountsPage() {
  const { accounts, account, accountId, setAccountId } = useAccount();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [createOpen, setCreateOpen] = useState(false);
  const [fundOpen, setFundOpen] = useState<"deposit" | "withdraw" | null>(null);
  const [amount, setAmount] = useState("10000");
  const [note, setNote] = useState("");

  // Group by currency instead of converting: there is no rate in the system, and
  // a made-up rate would silently misstate the user's total.
  const currencyTotals = useMemo(() => {
    const totals = new Map<string, { currency: string; total: number; count: number }>();
    for (const item of accounts) {
      const entry = totals.get(item.base_currency) ?? {
        currency: item.base_currency,
        total: 0,
        count: 0,
      };
      entry.total += item.cash_balance;
      entry.count += 1;
      totals.set(item.base_currency, entry);
    }
    return [...totals.values()].sort((a, b) => b.total - a.total);
  }, [accounts]);

  const ledger = useQuery({
    queryKey: ["ledger", accountId],
    queryFn: () => api.ledger(accountId as string, 100),
    enabled: Boolean(accountId),
  });

  const fund = useMutation({
    mutationFn: () =>
      fundOpen === "deposit"
        ? api.deposit(accountId as string, Number(amount), note)
        : api.withdraw(accountId as string, Number(amount), note),
    onSuccess: () => {
      toast.push(fundOpen === "deposit" ? "入金成功" : "出金成功");
      queryClient.invalidateQueries();
      setFundOpen(null);
      setNote("");
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const toggleStatus = useMutation({
    mutationFn: (status: "active" | "archived") => api.updateAccount(accountId as string, { status }),
    onSuccess: (_account, status) => {
      // Archiving stops every running instance on this account server-side;
      // saying so confirms the side effect rather than leaving it a surprise.
      toast.push(
        status === "archived"
          ? "账户已归档，该账户下的自动运行实例已停止"
          : "账户已恢复；自动运行实例需要手动重新启动",
      );
      queryClient.invalidateQueries();
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  // Native confirm() blocks the whole page (and any automation driving it), so
  // the warning lives in the app's own modal like every other confirmation.
  const [archiveOpen, setArchiveOpen] = useState(false);

  const archiveAccount = () => {
    if (!account) return;
    if (account.status !== "active") {
      toggleStatus.mutate("active");
      return;
    }
    setArchiveOpen(true);
  };

  return (
    <div className="stack" style={{ gap: 16 }}>
      {account ? (
        <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1.6fr) minmax(0, 1fr)" }}>
          <Card
            title={account.name}
            subtitle={`${account.kind === "paper" ? "模拟账户" : "实盘账户"} · 创建于 ${dateTimeLabel(account.created_at)}`}
            actions={
              <>
                <Badge tone={account.status === "active" ? "green" : "amber"}>
                  {account.status === "active" ? "运行中" : "已归档"}
                </Badge>
                <Badge tone="indigo">{account.base_currency}</Badge>
              </>
            }
          >
            <div className="grid" style={{ gridTemplateColumns: "repeat(3, 1fr)", gap: 14 }}>
              <MiniStat label="现金余额" value={cash(account.cash_balance, account.base_currency)} />
              <MiniStat label="初始资金" value={cash(account.initial_balance, account.base_currency)} />
              <MiniStat label="杠杆" value={`${account.leverage.toFixed(0)}x`} />
            </div>
            <div className="row" style={{ marginTop: 18, gap: 9 }}>
              <ButtonField onClick={() => setFundOpen("deposit")}>入金</ButtonField>
              <ButtonField variant="default" onClick={() => setFundOpen("withdraw")}>
                出金
              </ButtonField>
              <div className="toolbar-spacer" />
              <ButtonField
                variant={account.status === "active" ? "danger" : "default"}
                onClick={archiveAccount}
                disabled={toggleStatus.isPending}
              >
                {account.status === "active" ? "归档账户" : "恢复账户"}
              </ButtonField>
            </div>
            <p className="helper" style={{ marginTop: 14, marginBottom: 0 }}>
              出入金只影响模拟现金余额，不改变已实现盈亏。
              归档后禁止下单与入金，并停止该账户下所有自动运行实例；历史数据仍可查询。
            </p>
          </Card>

          <Card title="账户列表" subtitle="切换活跃账户" padded={false}>
            <div
              className="row"
              style={{
                gap: 16,
                padding: "10px 18px",
                borderBottom: "1px solid var(--border)",
                fontSize: 12.5,
                flexWrap: "wrap",
              }}
            >
              <span className="helper">合计</span>
              {/* Currencies are never summed together: adding CNY to USDT with no
                  exchange rate would produce a meaningless number. */}
              {currencyTotals.map((entry) => (
                <span key={entry.currency} className="num" style={{ fontWeight: 600 }}>
                  {cash(entry.total, entry.currency)}
                  <span className="muted" style={{ fontWeight: 400 }}>
                    {" "}
                    · {entry.count} 个账户
                  </span>
                </span>
              ))}
            </div>
            <div>
              {accounts.map((item, index, list) => (
                <button
                  key={item.id}
                  onClick={() => setAccountId(item.id)}
                  style={{
                    width: "100%",
                    textAlign: "left",
                    padding: "12px 18px",
                    background: item.id === accountId ? "var(--accent-soft)" : "transparent",
                    border: "none",
                    borderBottom: index < list.length - 1 ? "1px solid var(--border)" : "none",
                  }}
                >
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <span style={{ fontWeight: 600, fontSize: 13 }}>{item.name}</span>
                    <Badge tone={item.kind === "paper" ? "indigo" : "amber"}>
                      {item.kind === "paper" ? "模拟" : "实盘"}
                    </Badge>
                  </div>
                  <div className="helper" style={{ marginTop: 3 }}>
                    现金 {cash(item.cash_balance, item.base_currency)} · {item.status === "active" ? "运行中" : "已归档"}
                  </div>
                </button>
              ))}
              <div style={{ padding: 14 }}>
                <ButtonField variant="default" size="small" onClick={() => setCreateOpen(true)}>
                  新建账户
                </ButtonField>
              </div>
            </div>
          </Card>
        </div>
      ) : (
        <Card>
          <EmptyState
            title="还没有账户"
            text="创建一个模拟账户用于策略研究与交易验证。"
            action={<button className="button primary" onClick={() => setCreateOpen(true)}>新建账户</button>}
          />
        </Card>
      )}

      <Card title="资金流水" subtitle="只增不改的账本，余额由流水累加" padded={false}>
        {ledger.isLoading ? (
          <Loader />
        ) : ledger.data && ledger.data.length > 0 ? (
          <div className="table-wrap" style={{ maxHeight: 460 }}>
            <table className="table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>类型</th>
                  <th className="numeric">变动</th>
                  <th className="numeric">变动后余额</th>
                  <th>备注</th>
                  <th>关联</th>
                </tr>
              </thead>
              <tbody>
                {ledger.data.map((entry) => (
                  <tr key={entry.id}>
                    <td className="mono muted">{dateTimeLabel(entry.created_at)}</td>
                    <td>
                      <Badge tone={entry.kind === "withdraw" || (entry.kind === "trade" && entry.amount < 0) ? "red" : entry.kind === "trade" ? "indigo" : "green"}>
                        {ledgerLabel(entry.kind)}
                      </Badge>
                    </td>
                    <td className={`numeric ${entry.amount >= 0 ? "up" : "down"}`}>{cashSigned(entry.amount, account?.base_currency)}</td>
                    <td className="numeric">{cash(entry.balance_after, account?.base_currency)}</td>
                    <td className="secondary" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis" }}>
                      {entry.note || "—"}
                    </td>
                    <td className="mono muted">{entry.ref ? `${entry.ref.slice(0, 8)}…` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="暂无流水" text="入金、出金与每笔成交都会生成一条不可篡改的账本记录。" />
        )}
      </Card>

      <CreateAccountModal open={createOpen} onClose={() => setCreateOpen(false)} />

      <Modal
        open={archiveOpen}
        title="归档账户"
        onClose={() => setArchiveOpen(false)}
        footer={
          <>
            <button className="button" onClick={() => setArchiveOpen(false)}>
              取消
            </button>
            <button
              className="button danger"
              disabled={toggleStatus.isPending}
              onClick={() => {
                toggleStatus.mutate("archived");
                setArchiveOpen(false);
              }}
            >
              {toggleStatus.isPending ? "归档中…" : "确认归档"}
            </button>
          </>
        }
      >
        <div className="stack" style={{ gap: 10, fontSize: 13 }}>
          <div>
            归档后该账户<strong>禁止下单与入金</strong>，
            所有运行中的<strong>自动运行实例会被停止</strong>。
          </div>
          <div className="muted">
            已有持仓不会自动平仓。历史订单、持仓与资金流水仍然可查询；
            恢复账户后需要手动重新启动实例。
          </div>
        </div>
      </Modal>

      <Modal
        open={fundOpen !== null}
        title={fundOpen === "deposit" ? "账户入金" : "账户出金"}
        onClose={() => setFundOpen(null)}
        footer={
          <>
            <button className="button" onClick={() => setFundOpen(null)}>
              取消
            </button>
            <button
              className="button primary"
              disabled={fund.isPending || Number(amount) <= 0}
              onClick={() => fund.mutate()}
            >
              {fund.isPending ? "处理中…" : fundOpen === "deposit" ? "确认入金" : "确认出金"}
            </button>
          </>
        }
      >
        <Field label="金额（USDT）">
          <input className="input num" type="number" min={1} value={amount} onChange={(event) => setAmount(event.target.value)} />
        </Field>
        <Field label="备注" hint="可选，会记录到资金流水">
          <input className="input" value={note} onChange={(event) => setNote(event.target.value)} maxLength={240} />
        </Field>
      </Modal>
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ padding: "12px 14px", background: "var(--surface-subtle)", borderRadius: 12, border: "1px solid var(--border)" }}>
      <div className="helper">{label}</div>
      <div className="num" style={{ fontSize: 18, fontWeight: 650, marginTop: 3 }}>
        {value}
      </div>
    </div>
  );
}

function ledgerLabel(kind: string): string {
  const map: Record<string, string> = {
    initial: "初始资金",
    deposit: "入金",
    withdraw: "出金",
    trade: "成交",
    fee: "手续费",
    pnl: "盈亏",
  };
  return map[kind] ?? kind;
}
