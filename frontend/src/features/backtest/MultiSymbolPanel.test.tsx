import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MultiSymbolPanel } from "./MultiSymbolPanel";
import type { StrategyMeta } from "../../shared/api";

const sma = {
  key: "sma_cross",
  name: "双均线交叉",
  category: "trend",
  description: "",
  tags: [],
  defaults: { fast: 10, slow: 30 },
  params_schema: {
    properties: {
      fast: { type: "integer", title: "快线周期", default: 10 },
      slow: { type: "integer", title: "慢线周期", default: 30 },
    },
  },
} as unknown as StrategyMeta;

const apiMock = vi.hoisted(() => ({
  symbols: vi.fn(),
  strategies: vi.fn(),
  multiBacktest: vi.fn(),
}));

vi.mock("../../shared/api", () => ({ api: apiMock }));

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MultiSymbolPanel
        strategy="sma_cross"
        timeframe="1h"
        meta={sma}
        onStrategyChange={() => undefined}
        onTimeframeChange={() => undefined}
        timeframeOptions={["15m", "30m", "1h", "4h", "1d"]}
      />
    </QueryClientProvider>,
  );
}

describe("MultiSymbolPanel", () => {
  it("locks the picker to one market after the first selection", async () => {
    // Mixing A-shares and crypto has no common timestamps, so the backend rejects
    // it; narrowing the list up front avoids configuring a request that cannot run.
    apiMock.strategies.mockResolvedValue([sma]);
    apiMock.symbols.mockResolvedValue([
      { code: "BTCUSDT", name: "Bitcoin", market: "crypto", base: "BTC", quote: "USDT" },
      { code: "ETHUSDT", name: "Ethereum", market: "crypto", base: "ETH", quote: "USDT" },
      { code: "600036.SH", name: "招商银行", market: "cn_stock", base: "600036", quote: "CNY" },
    ]);
    const { container } = renderPanel();

    await waitFor(() => expect(container.querySelectorAll("tbody tr").length).toBe(3));
    const rows = [...container.querySelectorAll("tbody tr")];
    fireEvent.click(rows[0]); // BTCUSDT
    await waitFor(() => expect(container.textContent).toContain("1 个"));
    // The A-share row disappears once the market is locked to crypto.
    await waitFor(() => expect(container.querySelectorAll("tbody tr").length).toBe(2));
  });

  it("requires at least two symbols before running", async () => {
    apiMock.strategies.mockResolvedValue([sma]);
    apiMock.symbols.mockResolvedValue([
      { code: "BTCUSDT", name: "Bitcoin", market: "crypto", base: "BTC", quote: "USDT" },
      { code: "ETHUSDT", name: "Ethereum", market: "crypto", base: "ETH", quote: "USDT" },
    ]);
    const { container } = renderPanel();
    await waitFor(() => expect(container.querySelectorAll("tbody tr").length).toBe(2));

    const button = [...container.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("回测"),
    ) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });
});
