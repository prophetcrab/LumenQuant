export function money(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

const CURRENCY_SYMBOL: Record<string, string> = {
  CNY: "¥",
  USD: "$",
  USDT: "$",
};

export function currencySymbol(code: string | null | undefined): string {
  return CURRENCY_SYMBOL[(code ?? "USDT").toUpperCase()] ?? "";
}

/** Amount with the account's currency symbol, e.g. "¥1,234.56" or "$1,234.56". */
export function cash(value: number | null | undefined, currency = "USDT", digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${currencySymbol(currency)}${money(value, digits)}`;
}

/** Signed amount for P&L displays. */
export function cashSigned(value: number | null | undefined, currency = "USDT", digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const sign = value >= 0 ? "+" : "-";
  return `${sign}${currencySymbol(currency)}${money(Math.abs(value), digits)}`;
}

export function compact(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)}B`;
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return value.toFixed(2);
}

export function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

export function signed(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value >= 0 ? "+" : ""}${money(value, digits)}`;
}

export function price(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const abs = Math.abs(value);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 3 : abs >= 0.01 ? 5 : 7;
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function qty(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-US", { maximumFractionDigits: 6 });
}

export function timeLabel(epochMs: number): string {
  const date = new Date(epochMs);
  return `${String(date.getMonth() + 1).padStart(2, "0")}/${String(date.getDate()).padStart(2, "0")} ${String(
    date.getHours(),
  ).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

export function dateTimeLabel(value: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return `${String(date.getMonth() + 1).padStart(2, "0")}/${String(date.getDate()).padStart(2, "0")} ${String(
    date.getHours(),
  ).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}:${String(date.getSeconds()).padStart(2, "0")}`;
}

export function timeOnly(value: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}:${String(
    date.getSeconds(),
  ).padStart(2, "0")}`;
}

export function categoryLabel(category: string): string {
  const map: Record<string, string> = {
    trend: "趋势",
    mean_reversion: "均值回归",
    momentum: "动量",
    breakout: "突破",
    benchmark: "基准",
  };
  return map[category] ?? category;
}

export function orderStatusLabel(status: string): string {
  const map: Record<string, string> = {
    new: "新建",
    open: "挂单中",
    partially_filled: "部分成交",
    filled: "已成交",
    cancelled: "已撤销",
    rejected: "已拒绝",
  };
  return map[status] ?? status;
}

export function orderStatusTone(status: string): "green" | "amber" | "red" | "indigo" | "" {
  if (status === "filled") return "green";
  if (status === "open" || status === "partially_filled" || status === "new") return "amber";
  if (status === "rejected") return "red";
  if (status === "cancelled") return "";
  return "indigo";
}

export function timeframeLabel(value: string): string {
  const map: Record<string, string> = {
    "15m": "15 分钟",
    "30m": "30 分钟",
    "1h": "1 小时",
    "4h": "4 小时",
    "1d": "1 天",
  };
  return map[value] ?? value;
}
