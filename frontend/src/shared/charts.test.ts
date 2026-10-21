import { describe, expect, it } from "vitest";
import { backtestEquityOption, equityOption } from "./charts";

type Formatter = (value: number) => string;

function tooltipFormatter(option: ReturnType<typeof backtestEquityOption>): Formatter {
  const tooltip = option.tooltip as { valueFormatter?: Formatter };
  if (!tooltip?.valueFormatter) throw new Error("chart has no value formatter");
  return tooltip.valueFormatter;
}

describe("backtestEquityOption currency", () => {
  const points = [
    { time: 1_700_000_000_000, equity: 100_000 },
    { time: 1_700_086_400_000, equity: 105_000 },
  ];

  it("uses the market's symbol in the tooltip", () => {
    // Regression: the formatter hard-coded "$", so A-share backtests showed yuan
    // amounts labelled as dollars.
    expect(tooltipFormatter(backtestEquityOption(points, undefined, "CNY"))(1234.5)).toBe(
      "¥1,234.50",
    );
    expect(tooltipFormatter(backtestEquityOption(points, undefined, "USDT"))(1234.5)).toBe(
      "$1,234.50",
    );
  });

  it("defaults to USDT when no currency is given", () => {
    expect(tooltipFormatter(backtestEquityOption(points))(10)).toBe("$10.00");
  });
});

describe("equityOption currency", () => {
  const points = [
    { time: "2026-01-01T00:00:00Z", equity: 100_000 },
    { time: "2026-01-02T00:00:00Z", equity: 101_000 },
  ];

  it("follows the account currency", () => {
    expect(tooltipFormatter(equityOption(points, undefined, "CNY"))(9999)).toBe("¥9,999.00");
    expect(tooltipFormatter(equityOption(points, undefined, "USDT"))(9999)).toBe("$9,999.00");
  });

  it("defaults to USDT when no currency is given", () => {
    expect(tooltipFormatter(equityOption(points))(1)).toBe("$1.00");
  });
});
