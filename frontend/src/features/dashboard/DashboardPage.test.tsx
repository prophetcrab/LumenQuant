import { describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { DashboardPage } from "./DashboardPage";
import type { Account, Snapshot } from "../../shared/api";

const baseAccount: Account = {
  id: "acc",
  name: "账户",
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

const snap = (symbol: string, market: "crypto" | "cn_stock"): Snapshot => ({
  symbol,
  price: 100,
  change_24h_pct: 0.5,
  high_24h: 101,
  low_24h: 99,
  volume_24h: 1000,
  time: 1_700_000_000_000,
  market,
  prev_close: 100,
  limit_up: 110,
  limit_down: 90,
});

const apiMock = vi.hoisted(() => ({
  portfolioOverview: vi.fn(),
  equityHistory: vi.fn(),
  snapshots: vi.fn(),
  orders: vi.fn(),
  symbols: vi.fn(),
  backtests: vi.fn(),
}));

let currentAccount: Account = baseAccount;

vi.mock("../../shared/api", () => ({ api: apiMock }));
vi.mock("../../shared/account-context", () => ({
  useAccount: () => ({
    account: currentAccount,
    accountId: currentAccount.id,
    loading: false,
    error: null,
    accounts: [currentAccount],
    setAccountId: vi.fn(),
    refresh: async () => undefined,
  }),
}));

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <DashboardPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

function prime() {
  apiMock.portfolioOverview.mockResolvedValue({
    account_id: "acc",
    account_name: "账户",
    equity: 100_000,
    cash: 100_000,
    available_cash: 100_000,
    positions_value: 0,
    exposure_pct: 0,
    unrealized_pnl: 0,
    realized_pnl: 0,
    total_return_pct: 0,
    net_contributions: 100_000,
    allocations: [],
  });
  apiMock.equityHistory.mockResolvedValue([]);
  apiMock.orders.mockResolvedValue([]);
  apiMock.symbols.mockResolvedValue([]);
  apiMock.backtests.mockResolvedValue([]);
  apiMock.snapshots.mockResolvedValue([
    snap("BTCUSDT", "crypto"),
    snap("600036.SH", "cn_stock"),
  ]);
}

describe("DashboardPage market-aware navigation", () => {
  it("sends an A-share account to the A-share market page", async () => {
    // "看行情" used to always link to /markets, which is crypto-only — an
    // A-share user landed on a market they cannot trade.
    prime();
    currentAccount = { ...baseAccount, market: "cn_stock", base_currency: "CNY" };
    const { container } = renderPage();

    await waitFor(() => expect(container.textContent).toContain("看 A 股"));
    const link = [...container.querySelectorAll("a")].find((a) =>
      a.textContent?.includes("看 A 股"),
    );
    expect(link?.getAttribute("href")).toBe("/cn");
    expect(container.textContent).toContain("A 股 24 小时涨跌");
  });

  it("keeps the crypto destination for a crypto account", async () => {
    prime();
    currentAccount = baseAccount;
    const { container } = renderPage();

    await waitFor(() => expect(container.textContent).toContain("看行情"));
    const link = [...container.querySelectorAll("a")].find((a) =>
      a.textContent?.includes("看行情"),
    );
    expect(link?.getAttribute("href")).toBe("/markets");
  });
});
