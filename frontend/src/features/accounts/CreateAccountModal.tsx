import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/api";
import { useAccount } from "../../shared/account-context";
import { useToast } from "../../shared/toast";
import { ButtonField } from "../../components/form";
import { Field, Modal } from "../../components/ui";

export function CreateAccountModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { setAccountId, refresh } = useAccount();
  const toast = useToast();
  const [name, setName] = useState("研究账户");
  const [balance, setBalance] = useState("100000");
  const [leverage, setLeverage] = useState("1");

  const mutation = useMutation({
    mutationFn: () =>
      api.createAccount({
        name: name.trim(),
        initial_balance: Number(balance),
        leverage: Number(leverage),
      }),
    onSuccess: async (account) => {
      toast.push(`账户「${account.name}」已创建`);
      await refresh();
      setAccountId(account.id);
      queryClient.invalidateQueries();
      onClose();
      setName("研究账户");
      setBalance("100000");
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  return (
    <Modal
      open={open}
      title="创建模拟账户"
      onClose={onClose}
      footer={
        <>
          <button className="button" onClick={onClose}>
            取消
          </button>
          <button
            className="button primary"
            disabled={mutation.isPending || !name.trim() || Number(balance) <= 0}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? "创建中…" : "创建账户"}
          </button>
        </>
      }
    >
      <Field label="账户名称" hint="用于区分不同研究方法或资金曲线">
        <input className="input" value={name} onChange={(event) => setName(event.target.value)} maxLength={64} />
      </Field>
      <Field label="初始资金（USDT）" hint="仅影响模拟账户，可随时出入金">
        <input
          className="input num"
          type="number"
          min={1}
          value={balance}
          onChange={(event) => setBalance(event.target.value)}
        />
      </Field>
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
