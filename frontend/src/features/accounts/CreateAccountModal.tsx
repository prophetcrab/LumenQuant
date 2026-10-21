import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/api";
import { useAccount } from "../../shared/account-context";
import { useToast } from "../../shared/toast";
import { ButtonField } from "../../components/form";
import { Field, Modal } from "../../components/ui";

type Kind = "paper" | "live";

export function CreateAccountModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { setAccountId, refresh } = useAccount();
  const toast = useToast();
  const [kind, setKind] = useState<Kind>("paper");
  const [market, setMarket] = useState<"crypto" | "cn_stock">("crypto");
  const [name, setName] = useState("研究账户");
  const [balance, setBalance] = useState("100000");
  // A-share board lots are 100 shares; 茅台 alone needs ~¥300k for one lot.
  const suggestedBalance = market === "cn_stock" ? "500000" : "100000";
  const [leverage, setLeverage] = useState("1");

  const mutation = useMutation({
    mutationFn: () =>
      api.createAccount({
        name: name.trim(),
        initial_balance: Number(balance),
        leverage: market === "cn_stock" ? 1 : Number(leverage),
        market,
        kind,
      }),
    onSuccess: async (account) => {
      toast.push(`账户「${account.name}」已创建`);
      await refresh();
      setAccountId(account.id);
      queryClient.invalidateQueries();
      onClose();
      setName("研究账户");
      setBalance("100000");
      setKind("paper");
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const isLive = kind === "live";

  return (
    <Modal
      open={open}
      title={
        isLive
          ? market === "cn_stock"
            ? "创建 A 股实盘账户"
            : "创建实盘账户"
          : market === "cn_stock"
            ? "创建 A 股模拟账户"
            : "创建模拟账户"
      }
      onClose={onClose}
      footer={
        <>
          <button className="button" onClick={onClose}>
            取消
          </button>
          <button
            className={isLive ? "button danger" : "button primary"}
            disabled={mutation.isPending || !name.trim() || Number(balance) <= 0}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? "创建中…" : isLive ? "创建实盘账户" : "创建账户"}
          </button>
        </>
      }
    >
      <Field label="账户类型" hint="实盘账户需要服务端开启 LQ_LIVE_TRADING_ENABLED">
        <select
          className="select"
          value={kind}
          onChange={(event) => setKind(event.target.value as Kind)}
        >
          <option value="paper">模拟（不接触真实资金）</option>
          <option value="live">实盘（真实资金）</option>
        </select>
      </Field>
      <Field label="市场" hint="A股账户为人民币现金账户，不支持杠杆与做空">
        <select
          className="select"
          value={market}
          onChange={(event) => {
            const next = event.target.value as "crypto" | "cn_stock";
            setMarket(next);
            setBalance(next === "cn_stock" ? "500000" : "100000");
          }}
        >
          <option value="crypto">加密货币（24/7）</option>
          <option value="cn_stock">A股（沪深，T+1）</option>
        </select>
      </Field>
      <Field label="账户名称" hint="用于区分不同研究方法或资金曲线">
        <input className="input" value={name} onChange={(event) => setName(event.target.value)} maxLength={64} />
      </Field>
      <Field
        label={market === "cn_stock" ? "初始资金（CNY）" : "初始资金（USDT）"}
        hint={isLive ? "实盘账户的现金以券商为准，此值仅作本地记账初始基准" : "仅影响模拟账户，可随时出入金"}
      >
        <input
          className="input num"
          type="number"
          min={1}
          value={balance}
          onChange={(event) => setBalance(event.target.value)}
          placeholder={suggestedBalance}
        />
      </Field>
      {market === "crypto" && !isLive ? (
        <Field label="杠杆" hint="1 倍为现货全额，最高 10 倍">
          <input
            className="input num"
            type="number"
            min={1}
            max={10}
            step={1}
            value={leverage}
            onChange={(event) => setLeverage(event.target.value)}
          />
        </Field>
      ) : null}
      {isLive ? (
        <div
          className="stack"
          style={{
            gap: 6,
            padding: "12px 14px",
            border: "1px solid var(--negative-soft, #f3c2c2)",
            background: "var(--negative-soft)",
            borderRadius: 10,
            fontSize: 12.5,
            lineHeight: 1.7,
          }}
        >
          <strong>实盘账户会产生真实资金变动。</strong>
          <span>
            手动下单仍需要请求头 <span className="mono">X-Live-Confirm: true</span>；
            让策略实例自动下单还需要额外开启
            <span className="mono"> LQ_LIVE_AUTO_TRADING_ENABLED</span>。
          </span>
          <span className="muted">
            本项目用于研究与教学，不构成投资建议。实盘交易可能导致资金损失。
          </span>
        </div>
      ) : null}
    </Modal>
  );
}

export function CreateAccountButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <ButtonField onClick={() => setOpen(true)}>新建账户</ButtonField>
      <CreateAccountModal open={open} onClose={() => setOpen(false)} />
    </>
  );
}
