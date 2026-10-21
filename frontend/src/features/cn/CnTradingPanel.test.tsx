import { describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CnTradingPanel } from "./CnTradingPanel";
import type { Account } from "../../shared/api";

const account: Account = {
  id: "acc-cn",
  name: "A股账户",
  kind: "paper",
  market: "cn_stock",
  broker: "paper",
  base_currency: "CNY",
  initial_balance: 500_000,
  leverage: 1,
  status: "active",
  cash_balance: 500_000,
  created_at: "2026-10-21T00:00:00Z",
  updated_at: "2026-10-21T00:00:00Z",
};

const apiMock = vi.hoisted(() => ({
  cnRules: vi.fn(),
  cnSession: vi.fn(),
  cnSymbols: vi.fn(),
  portfolioOverview: vi.fn(),
  positions: vi.fn(),
  orders: vi.fn(),
  brokerStatus: vi.fn(),
  thsHealth: vi.fn(),
  placeOrder: vi.fn(),
  settleT1: vi.fn(),
  cancelOrder: vi.fn(),
}));

vi.mock("../../shared/api", () => ({ api: apiMock }));
vi.mock("../../shared/toast", () => ({ useToast: () => ({ push: vi.fn() }) }));
vi.mock("../../shared/account-context", () => ({
  useAccount: () => ({ account, accountId: account.id }),
}));

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <CnTradingPanel />
    </QueryClientProvider>,
  );
}

const closedSession = {
  open: false,
  day: "2026-10-24",
  session: "closed",
  session_open: 0,
  session_close: 0,
  next_open: 1_792_800_000_000,
};

const openSession = { ...closedSession, open: true, session: "morning" };

function prime() {
  apiMock.cnRules.mockResolvedValue({
    lot_size: 100,
    t_plus_one: true,
    fees: {
      commission_bps: 2.5,
      min_commission: 5,
      stamp_duty_bps: 5,
      transfer_fee_bps: 0.1,
    },
    boards: {},
  });
  apiMock.cnSymbols.mockResolvedValue([
    {
      code: "600036.SH",
      name: "招商银行",
      price: 42.0,
      change_pct: 0.5,
      limit_up: 46.2,
      limit_down: 37.8,
    },
  ]);
  apiMock.portfolioOverview.mockResolvedValue({
    equity: 500_000,
    cash: 500_000,
    available_cash: 500_000,
    reserved_cash: 0,
    positions_value: 0,
    exposure_pct: 0,
    unrealized_pnl: 0,
    position_count: 0,
    net_contributions: 500_000,
    total_return_pct: 0,
    allocations: [],
  });
  apiMock.positions.mockResolvedValue([]);
  apiMock.orders.mockResolvedValue([]);
  apiMock.brokerStatus.mockResolvedValue({ ok: false });
  apiMock.thsHealth.mockResolvedValue({ ok: false, gateway: "" });
}

describe("CnTradingPanel closed-session guidance", () => {
  it("explains that paper orders still fill while the market is closed", async () => {
    // Paper accounts bypass the session gate by default and fill at the last
    // price. Without this note the natural assumption is "queued until open".
    prime();
    apiMock.cnSession.mockResolvedValue(closedSession);
    const { container } = renderPanel();

    await waitFor(() => expect(container.textContent).toContain("当前休市"));
    expect(container.textContent).toContain("模拟账户仍可下单");
    expect(container.textContent).toContain("实盘会被交易时段校验拒绝");
  });

  it("shows nothing extra while the market is open", async () => {
    prime();
    apiMock.cnSession.mockResolvedValue(openSession);
    const { container } = renderPanel();

    await waitFor(() => expect(container.textContent).toContain("招商银行"));
    expect(container.textContent).not.toContain("当前休市");
  });
});
