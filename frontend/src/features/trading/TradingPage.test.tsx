import { describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TradingPage } from "./TradingPage";
import type { Account, Snapshot } from "../../shared/api";

const cryptoAccount: Account = {
  id: "acc-crypto",
  name: "加密账户",
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

const snap = (symbol: string, market: "crypto" | "cn_stock", p: number): Snapshot => ({
  symbol,
  price: p,
  change_24h_pct: 0,
  high_24h: p,
  low_24h: p,
  volume_24h: 1000,
  time: 1_700_000_000_000,
  market,
  prev_close: p,
  limit_up: p * 1.1,
  limit_down: p * 0.9,
  suspended: false,
});

const apiMock = vi.hoisted(() => ({
  snapshots: vi.fn(),
  positions: vi.fn(),
  orders: vi.fn(),
  fills: vi.fn(),
  portfolioOverview: vi.fn(),
  placeOrder: vi.fn(),
}));

let currentAccount: Account = cryptoAccount;

vi.mock("../../shared/api", () => ({ api: apiMock }));
vi.mock("../../shared/toast", () => ({ useToast: () => ({ push: vi.fn() }) }));
vi.mock("../../shared/account-context", () => ({
  useAccount: () => ({ account: currentAccount, accountId: currentAccount.id }),
}));
vi.mock("../cn/CnTradingPanel", () => ({
  CnTradingPanel: () => <div data-testid="cn-panel" />,
}));

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TradingPage />
    </QueryClientProvider>,
  );
}

describe("TradingPage symbol scope", () => {
  it("asks the API for the account's market and defaults to its benchmark", async () => {
    // The server does the filtering, so the contract to pin is the query itself:
    // calling snapshots() with no market returned all fourteen instruments and a
    // crypto account could submit an A-share order that failed with 422.
    apiMock.snapshots.mockImplementation(async (market?: string) => {
      if (market === "crypto") {
        return [snap("ADAUSDT", "crypto", 0.4), snap("BTCUSDT", "crypto", 110_000)];
      }
      return [
        snap("ADAUSDT", "crypto", 0.4),
        snap("BTCUSDT", "crypto", 110_000),
        snap("600036.SH", "cn_stock", 42),
      ];
    });
    apiMock.positions.mockResolvedValue([]);
    apiMock.orders.mockResolvedValue([]);
    apiMock.fills.mockResolvedValue([]);
    apiMock.portfolioOverview.mockResolvedValue({
      equity: 100_000,
      cash: 100_000,
      available_cash: 100_000,
      positions_value: 0,
      exposure_pct: 0,
      unrealized_pnl: 0,
      position_count: 0,
      net_contributions: 100_000,
      total_return_pct: 0,
      allocations: [],
    });
    currentAccount = cryptoAccount;
    renderPage();

    await waitFor(() => expect(apiMock.snapshots).toHaveBeenCalledWith("crypto"));
    await waitFor(() => {
      const el = document.querySelector("select") as HTMLSelectElement | null;
      expect(el?.options.length ?? 0).toBeGreaterThan(0);
    });
    const select = document.querySelector("select") as HTMLSelectElement;
    const values = [...select.options].map((option) => option.value);
    expect(values).toEqual(["ADAUSDT", "BTCUSDT"]);
    expect(select.value).toBe("BTCUSDT");
  });
});
