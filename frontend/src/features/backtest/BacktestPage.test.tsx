import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BacktestPage } from "./BacktestPage";
import type { StrategyMeta } from "../../shared/api";

// The library hands the backtest page a tuned configuration through the URL.
// These tests pin the two failures that actually happened during development:
// StrictMode wiping the parameters, and a refresh re-applying stale values.
const smaCross = {
  key: "sma_cross",
  name: "双均线交叉",
  category: "trend",
  description: "",
  tags: [],
  defaults: { fast: 10, slow: 30, regime_period: 200, require_regime: false },
  params_schema: {
    properties: {
      fast: { type: "integer", title: "快线周期", default: 10 },
      slow: { type: "integer", title: "慢线周期", default: 30 },
      regime_period: { type: "integer", title: "牛熊过滤均线", default: 200 },
      require_regime: { type: "boolean", title: "只在长期均线上方做多", default: false },
    },
  },
} as unknown as StrategyMeta;

const rsi = {
  key: "rsi_reversion",
  name: "RSI 均值回归",
  category: "mean_reversion",
  description: "",
  tags: [],
  defaults: { period: 14, oversold: 30, overbought: 70 },
  params_schema: {
    properties: {
      period: { type: "integer", title: "RSI 周期", default: 14 },
      oversold: { type: "integer", title: "超卖阈值", default: 30 },
      overbought: { type: "integer", title: "超买阈值", default: 70 },
    },
  },
} as unknown as StrategyMeta;

const apiMock = vi.hoisted(() => ({
  strategies: vi.fn(),
  symbols: vi.fn(),
  backtests: vi.fn(),
  runBacktest: vi.fn(),
  deleteBacktest: vi.fn(),
  backtest: vi.fn(),
  portfolioBacktest: vi.fn(),
}));

vi.mock("../../shared/api", () => ({ api: apiMock }));
// The page reads the selected account only to pick a market-appropriate default
// symbol; no account means "leave the explicit choice alone".
let currentAccount: { market: "crypto" | "cn_stock"; base_currency: string } | null = null;
vi.mock("../../shared/account-context", () => ({
  useAccount: () => ({ account: currentAccount, accountId: undefined }),
}));
// ECharts needs a real canvas; the panels below the form are out of scope here.
vi.mock("../../shared/echarts", () => ({
  Chart: () => null,
}));

function renderAt(url: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <StrictMode>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[url]}>
          <Routes>
            <Route path="/backtest" element={<BacktestPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </StrictMode>,
  );
}

const numberField = (label: string) => screen.getByLabelText(label) as HTMLInputElement;

describe("BacktestPage strategy handoff", () => {
  beforeEach(() => {
    apiMock.strategies.mockResolvedValue([smaCross, rsi]);
    apiMock.symbols.mockResolvedValue([
      { code: "600036.SH", market: "cn_stock" },
      { code: "BTCUSDT", market: "crypto" },
      { code: "ETHUSDT", market: "crypto" },
    ]);
    currentAccount = null;
    apiMock.backtests.mockResolvedValue([]);
  });

  it("prefills strategy, symbol, timeframe and parameters from the URL", async () => {
    renderAt(
      "/backtest?strategy=sma_cross&symbol=600036.SH&timeframe=1d&params=" +
        encodeURIComponent(JSON.stringify({ fast: 7, slow: 55, require_regime: true })),
    );

    await waitFor(() => expect(numberField("快线周期").value).toBe("7"));
    expect(numberField("慢线周期").value).toBe("55");
    expect(numberField("牛熊过滤均线").value).toBe("200"); // schema default fills the gap
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("标的") as HTMLSelectElement).value).toBe("600036.SH");
    expect((screen.getByLabelText("周期") as HTMLSelectElement).value).toBe("1d");
  });

  it("keeps handed-over values under StrictMode's double effect pass", async () => {
    // Regression: the first pass consumed the handoff and the second overwrote
    // the merged parameters with schema defaults, so 7/55 silently became 10/30.
    renderAt(
      "/backtest?strategy=sma_cross&params=" +
        encodeURIComponent(JSON.stringify({ fast: 21, slow: 89 })),
    );

    await waitFor(() => expect(numberField("快线周期").value).toBe("21"));
    // Give a second effect pass a chance to clobber it, then re-assert.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(numberField("快线周期").value).toBe("21");
    expect(numberField("慢线周期").value).toBe("89");
  });

  it("strips the query so a remount does not re-apply stale parameters", async () => {
    const { unmount } = renderAt(
      "/backtest?strategy=sma_cross&params=" +
        encodeURIComponent(JSON.stringify({ fast: 7 })),
    );
    await waitFor(() => expect(numberField("快线周期").value).toBe("7"));
    await waitFor(() => expect(window.location.search).toBe(""));
    unmount();
  });

  it("falls back to the default strategy when the URL has no handoff", async () => {
    renderAt("/backtest");
    await waitFor(() => expect(numberField("快线周期").value).toBe("10"));
    expect((screen.getByLabelText("策略") as HTMLSelectElement).value).toBe("sma_cross");
  });

  it("resets to the new strategy's defaults when the user switches strategy", async () => {
    renderAt("/backtest");
    await waitFor(() => expect(numberField("快线周期").value).toBe("10"));

    const select = screen.getByLabelText("策略") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "rsi_reversion" } });

    await waitFor(() => expect(screen.getByLabelText("RSI 周期")).toBeTruthy());
    expect((screen.getByLabelText("RSI 周期") as HTMLInputElement).value).toBe("14");
    expect(screen.queryByLabelText("快线周期")).toBeNull();
  });

  it("defaults to a symbol from the account's market", async () => {
    // Research is not account-bound, so every symbol stays selectable; only the
    // preselected value follows the account, to avoid opening an A-share account
    // on a crypto backtest.
    currentAccount = { market: "cn_stock", base_currency: "CNY" };
    renderAt("/backtest");
    await waitFor(() => expect((screen.getByLabelText("标的") as HTMLSelectElement).value).toBe("600036.SH"));
    currentAccount = null;
  });

  it("keeps the symbol handed over from the strategy library", async () => {
    // The user picked it deliberately; a market-based default must not override it.
    currentAccount = { market: "cn_stock", base_currency: "CNY" };
    renderAt("/backtest?strategy=sma_cross&symbol=ETHUSDT");
    await waitFor(() => expect(numberField("快线周期").value).toBe("10"));
    expect((screen.getByLabelText("标的") as HTMLSelectElement).value).toBe("ETHUSDT");
    currentAccount = null;
  });
});
