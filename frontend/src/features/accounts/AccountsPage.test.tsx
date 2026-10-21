import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AccountsPage } from "./AccountsPage";
import type { Account } from "../../shared/api";

const apiMock = vi.hoisted(() => ({
  ledger: vi.fn(),
  updateAccount: vi.fn(),
  deposit: vi.fn(),
  withdraw: vi.fn(),
}));

const account: Account = {
  id: "acc-1",
  name: "归档测试",
  kind: "paper",
  market: "crypto",
  broker: "paper",
  base_currency: "USDT",
  initial_balance: 100_000,
  leverage: 1,
  status: "active",
  cash_balance: 100_000,
  created_at: "2026-10-21T00:00:00Z",
  updated_at: "2026-10-21T00:00:00Z",
};

vi.mock("../../shared/api", () => ({ api: apiMock }));
vi.mock("../../shared/toast", () => ({ useToast: () => ({ push: vi.fn() }) }));
vi.mock("../../shared/account-context", () => ({
  useAccount: () => ({
    accounts: [account],
    account,
    accountId: account.id,
    setAccountId: vi.fn(),
    refresh: async () => undefined,
  }),
}));

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AccountsPage />
    </QueryClientProvider>,
  );
}

describe("AccountsPage archive flow", () => {
  it("warns that archiving stops instances before doing it", async () => {
    apiMock.ledger.mockResolvedValue([]);
    renderPage();

    // Archiving must not happen on the first click: it silently stops every
    // running instance server-side, so the consequence has to be stated first.
    fireEvent.click(screen.getByRole("button", { name: "归档账户" }));
    await waitFor(() => expect(screen.getByText(/所有运行中的/)).toBeTruthy());
    expect(screen.getByText("禁止下单与入金")).toBeTruthy();
    expect(screen.getByText(/已有持仓不会自动平仓/)).toBeTruthy();
    expect(apiMock.updateAccount).not.toHaveBeenCalled();

    const confirm = screen.getByRole("button", { name: "确认归档" });
    expect(confirm.className).toContain("danger");
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(apiMock.updateAccount).toHaveBeenCalledWith("acc-1", { status: "archived" }),
    );
  });

  it("cancelling leaves the account untouched", async () => {
    apiMock.ledger.mockResolvedValue([]);
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "归档账户" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "取消" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByText(/所有运行中的/)).toBeNull());
    expect(apiMock.updateAccount).not.toHaveBeenCalled();
  });

  it("states the side effects in the card copy", () => {
    apiMock.ledger.mockResolvedValue([]);
    renderPage();
    expect(screen.getByText(/并停止该账户下所有自动运行实例/)).toBeTruthy();
  });
});
