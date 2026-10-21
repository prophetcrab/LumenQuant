import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AlertsPage } from "./AlertsPage";
import type { Account, Alert } from "../../shared/api";

const account: Account = {
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

const breaker: Alert = {
  id: "a1",
  account_id: "acc",
  level: "critical",
  category: "breaker",
  title: "风控熔断已触发，新开仓被禁止",
  message: "daily loss limit hit (-9.92% <= -5.00%)",
  ref_type: "account",
  ref_id: "acc",
  acknowledged: false,
  acknowledged_at: "",
  created_at: "2026-10-21T14:16:45Z",
};

const apiMock = vi.hoisted(() => ({
  alerts: vi.fn(),
  alertSummary: vi.fn(),
  ackAlert: vi.fn(),
  ackAllAlerts: vi.fn(),
}));

vi.mock("../../shared/api", () => ({ api: apiMock }));
vi.mock("../../shared/toast", () => ({ useToast: () => ({ push: vi.fn() }) }));
vi.mock("../../shared/account-context", () => ({
  useAccount: () => ({ account, accountId: account.id }),
}));

const oneCritical = {
  unacknowledged: 1,
  by_level: { info: 0, warning: 0, critical: 1 },
};

beforeEach(() => {
  // Implementations are set here because the global config runs
  // `restoreMocks: true`, which clears them between tests.
  apiMock.alertSummary.mockImplementation(() => Promise.resolve(oneCritical));
  apiMock.alerts.mockImplementation(() => Promise.resolve([breaker]));
  apiMock.ackAlert.mockImplementation(() =>
    Promise.resolve({ ...breaker, acknowledged: true }),
  );
  apiMock.ackAllAlerts.mockImplementation(() => Promise.resolve({ acknowledged: 1 }));
});

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AlertsPage />
    </QueryClientProvider>,
  );
}

describe("AlertsPage", () => {
  it("shows a pending alert with a translated category", async () => {
    const { container } = renderPage();

    await waitFor(() => expect(container.textContent).toContain("风控熔断已触发"));
    expect(container.textContent).toContain("严重");
    expect(container.textContent).toContain("daily loss limit hit");
    // The stat card reflects the summary, not the row count.
    expect(container.textContent).toContain("待确认");
  });

  it("acknowledges a single alert", async () => {
    const { container } = renderPage();

    await waitFor(() => expect(container.textContent).toContain("风控熔断已触发"));
    const button = [...container.querySelectorAll("button")].find(
      (item) => item.textContent?.trim() === "确认",
    );
    expect(button).toBeTruthy();
    fireEvent.click(button as HTMLButtonElement);
    await waitFor(() => expect(apiMock.ackAlert).toHaveBeenCalledWith("a1"));
  });

  it("disables bulk acknowledge when nothing is pending", async () => {
    apiMock.alertSummary.mockImplementation(() =>
      Promise.resolve({ unacknowledged: 0, by_level: { info: 0, warning: 0, critical: 0 } }),
    );
    apiMock.alerts.mockImplementation(() => Promise.resolve([]));
    const { container } = renderPage();

    await waitFor(() => expect(container.textContent).toContain("没有待确认的告警"));
    const all = [...container.querySelectorAll("button")].find((item) =>
      item.textContent?.includes("全部确认"),
    ) as HTMLButtonElement;
    expect(all.disabled).toBe(true);
  });
});
