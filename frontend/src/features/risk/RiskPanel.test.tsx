import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RiskPanel } from "./RiskPanel";
import type { InputHTMLAttributes, ReactNode } from "react";

const account = {
  id: "acc",
  name: "币安实盘",
  kind: "live" as const,
  market: "crypto" as const,
  broker: "binance",
  base_currency: "USDT",
  initial_balance: 1,
  leverage: 1,
  status: "active" as const,
  cash_balance: 100_000,
  created_at: "2026-10-21T00:00:00Z",
  updated_at: "2026-10-21T00:00:00Z",
};

const apiMock = vi.hoisted(() => ({
  riskLimits: vi.fn(),
  riskState: vi.fn(),
  resetRisk: vi.fn(),
  brokerStatus: vi.fn(),
}));

vi.mock("../../shared/api", () => ({ api: apiMock }));
vi.mock("../../shared/toast", () => ({ useToast: () => ({ push: vi.fn() }) }));
vi.mock("../../shared/account-context", () => ({
  useAccount: () => ({ account, accountId: account.id }),
}));
// The shared UI module pulls in form primitives that are irrelevant here.
vi.mock("../../components/ui", () => ({
  Card: ({ title, subtitle, children }: { title: string; subtitle?: string; children?: ReactNode }) => (
    <section>
      <h2>{title}</h2>
      {subtitle ? <p>{subtitle}</p> : null}
      {children}
    </section>
  ),
  Badge: ({ children }: { children?: ReactNode }) => <span>{children}</span>,
  Loader: () => <span>loading</span>,
  Stat: ({ label, value }: { label: string; value: string }) => (
    <div>
      <span>{label}</span>
      <span>{value}</span>
    </div>
  ),
  Field: ({ label, children }: { label: string; children?: ReactNode }) => (
    <label>
      {label}
      {children}
    </label>
  ),
  Modal: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  EmptyState: ({ title }: { title: string }) => <div>{title}</div>,
  Input: (props: InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

const readyStatus = {
  live_trading_enabled: true,
  live_auto_trading_enabled: true,
  reconciler_running: true,
  reconcile_seconds: 3,
  channels: {
    binance: { market: "crypto", ready: true, credentials_configured: true },
    ths: { market: "cn_stock", ready: false, gateway_configured: false, mock_enabled: false },
  },
};

beforeEach(() => {
  apiMock.riskLimits.mockImplementation(() =>
    Promise.resolve({
      max_order_notional: 1_000_000,
      max_position_notional: 5_000_000,
      min_order_notional: 10,
      max_orders_per_minute: 120,
      max_daily_loss_pct: 0.05,
      max_drawdown_pct: 0.25,
    }),
  );
  apiMock.riskState.mockImplementation(() =>
    Promise.resolve({
      account_id: account.id,
      breaker_active: false,
      breaker_reason: "",
      halt_kind: "",
      day_start_equity: 100_000,
      peak_equity: 100_000,
      daily_reason: "",
      drawdown_reason: "",
      risk_day: "2026-10-21",
    }),
  );
  apiMock.brokerStatus.mockImplementation(() => Promise.resolve(readyStatus));
});

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <RiskPanel />
    </QueryClientProvider>,
  );
}

describe("RiskPanel live channels", () => {
  it("shows each market channel from the channels map", async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByText("币安（加密）")).toBeTruthy());

    // Both markets are listed, with their own readiness — the old card read a
    // single `broker` field that the API no longer returns.
    expect(screen.getByText("同花顺（A股）")).toBeTruthy();
    expect(screen.getByText("就绪")).toBeTruthy();
    expect(screen.getByText("未就绪")).toBeTruthy();
  });

  it("reports the fill-polling loop, not just credentials", async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByText("成交回报轮询")).toBeTruthy());
    expect(screen.getByText("运行中 · 每 3s")).toBeTruthy();
  });

  it("flags a stopped reconciler so live orders are not assumed to fill", async () => {
    apiMock.brokerStatus.mockImplementation(() =>
      Promise.resolve({ ...readyStatus, reconciler_running: false }),
    );
    renderPanel();
    await waitFor(() => expect(screen.getByText("成交回报轮询")).toBeTruthy());
    expect(screen.getByText("未运行")).toBeTruthy();
  });
});
