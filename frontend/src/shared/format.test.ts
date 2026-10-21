import { describe, expect, it } from "vitest";
import { cash, cashSigned, compact, currencySymbol, money, pct, price, qty } from "./format";

describe("currencySymbol", () => {
  it("maps the traded currencies", () => {
    expect(currencySymbol("CNY")).toBe("¥");
    expect(currencySymbol("USD")).toBe("$");
    expect(currencySymbol("USDT")).toBe("$");
  });

  it("defaults to USDT when the account has no currency", () => {
    expect(currencySymbol(null)).toBe("$");
    expect(currencySymbol(undefined)).toBe("$");
  });

  it("returns no symbol for an unknown currency instead of guessing", () => {
    // Printing "$" for an unconfigured currency would silently mislabel amounts.
    expect(currencySymbol("EUR")).toBe("");
  });
});

describe("cash", () => {
  it("prefixes the account currency", () => {
    expect(cash(1234.5, "CNY")).toBe("¥1,234.50");
    expect(cash(1234.5, "USDT")).toBe("$1,234.50");
  });

  it("renders a dash for missing values", () => {
    expect(cash(null, "CNY")).toBe("—");
    expect(cash(NaN, "CNY")).toBe("—");
  });

  it("keeps negative amounts inside the symbol", () => {
    expect(cash(-99.005, "CNY")).toBe("¥-99.01");
  });
});

describe("cashSigned", () => {
  it("always shows a sign", () => {
    expect(cashSigned(12, "CNY")).toBe("+¥12.00");
    expect(cashSigned(-12, "CNY")).toBe("-¥12.00");
  });

  it("treats zero as positive", () => {
    expect(cashSigned(0, "USDT")).toBe("+$0.00");
  });
});

describe("pct", () => {
  it("adds a plus sign only for non-negative values", () => {
    expect(pct(1.234)).toBe("+1.23%");
    expect(pct(-1.234)).toBe("-1.23%");
    expect(pct(0)).toBe("+0.00%");
  });

  it("honours the requested precision", () => {
    expect(pct(12.3456, 3)).toBe("+12.346%");
  });
});

describe("magnitude helpers", () => {
  it("price adapts precision to the magnitude", () => {
    expect(price(1312.5)).toBe("1,312.50");
    expect(price(42.25)).toBe("42.250");
    expect(price(0.0001234)).toBe("0.0001234");
  });

  it("qty trims trailing zeros", () => {
    expect(qty(100)).toBe("100");
    expect(qty(0.5)).toBe("0.5");
  });

  it("compact abbreviates large numbers", () => {
    expect(compact(1_500_000_000)).toBe("1.50B");
    expect(compact(2_500_000)).toBe("2.50M");
    expect(compact(3_400)).toBe("3.4K");
  });

  it("money fixes the digit count", () => {
    expect(money(7, 4)).toBe("7.0000");
  });
});
