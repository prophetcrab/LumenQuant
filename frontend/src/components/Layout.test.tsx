import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { Layout } from "./Layout";

const apiMock = vi.hoisted(() => ({
  alertSummary: vi.fn(),
  accounts: vi.fn(),
}));

vi.mock("../shared/api", () => ({
  api: apiMock,
  health: async () => ({ status: "ok", version: "0.1.0" }),
}));

vi.mock("../shared/account-context", () => ({
  useAccount: () => ({
    accounts: [
      {
        id: "acc",
        name: "账户",
        kind: "paper",
        market: "crypto",
        base_currency: "USDT",
      },
    ],
    accountId: "acc",
    setAccountId: vi.fn(),
  }),
}));

vi.mock("../shared/hooks", () => ({
  useMarketClock: () => ({ data: undefined }),
}));

beforeEach(() => {
  apiMock.alertSummary.mockImplementation(() =>
    Promise.resolve({ unacknowledged: 0, by_level: {} }),
  );
});

function renderLayout() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <Layout>
          <div>content</div>
        </Layout>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("Layout alert badge", () => {
  it("shows the unacknowledged count next to the alerts link", async () => {
    apiMock.alertSummary.mockImplementation(() =>
      Promise.resolve({ unacknowledged: 3, by_level: { critical: 3 } }),
    );
    const { container } = renderLayout();

    await waitFor(() => expect(container.querySelector(".nav-badge")?.textContent).toBe("3"));
    // The badge is a cross-account total: a breaker on another account is
    // exactly what the operator must not have to go looking for.
    expect(container.querySelector(".nav-badge")?.getAttribute("aria-label")).toBe(
      "3 条待确认（全部账户）",
    );
    expect(apiMock.alertSummary).toHaveBeenCalledWith(undefined, {
      allAccounts: true,
    });
  });

  it("hides the badge when nothing is pending", async () => {
    const { container } = renderLayout();
    // Give the query a beat to resolve before asserting absence.
    await waitFor(() => expect(apiMock.alertSummary).toHaveBeenCalled());
    expect(container.querySelector(".nav-badge")).toBeNull();
  });

  it("caps the displayed count at 99+", async () => {
    apiMock.alertSummary.mockImplementation(() =>
      Promise.resolve({ unacknowledged: 250, by_level: { critical: 250 } }),
    );
    const { container } = renderLayout();
    await waitFor(() => expect(container.querySelector(".nav-badge")?.textContent).toBe("99+"));
  });
});
