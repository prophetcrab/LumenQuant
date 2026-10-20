import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { Account } from "./api";

type AccountContextValue = {
  accounts: Account[];
  account: Account | null;
  accountId: string | undefined;
  setAccountId: (id: string) => void;
  refresh: () => Promise<unknown>;
  loading: boolean;
  error: Error | null;
};

const AccountContext = createContext<AccountContextValue | null>(null);
const STORAGE_KEY = "lumenquant.account";

export function AccountProvider({ children }: { children: ReactNode }) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["accounts"],
    queryFn: api.accounts,
    refetchInterval: 15_000,
  });
  const [selected, setSelected] = useState<string | null>(
    () => localStorage.getItem(STORAGE_KEY),
  );

  const accounts = useMemo(() => data ?? [], [data]);

  useEffect(() => {
    if (accounts.length === 0) {
      if (selected) setSelected(null);
      return;
    }
    const exists = accounts.some((item) => item.id === selected);
    if (!exists && accounts[0]) setSelected(accounts[0].id);
  }, [accounts, selected]);

  useEffect(() => {
    if (selected) localStorage.setItem(STORAGE_KEY, selected);
    else localStorage.removeItem(STORAGE_KEY);
  }, [selected]);

  const setAccountId = useCallback((id: string) => setSelected(id), []);
  const account = accounts.find((item) => item.id === selected) ?? null;

  const value = useMemo(
    () => ({
      accounts,
      account,
      accountId: account?.id,
      setAccountId,
      refresh: refetch,
      loading: isLoading,
      error: (error as Error | null) ?? null,
    }),
    [accounts, account, setAccountId, refetch, isLoading, error],
  );

  return <AccountContext.Provider value={value}>{children}</AccountContext.Provider>;
}

export function useAccount(): AccountContextValue {
  const context = useContext(AccountContext);
  if (!context) throw new Error("useAccount must be used inside AccountProvider");
  return context;
}
