import type { ParamValue } from "./strategy-params";

export type StrategyHandoff = {
  strategy: string;
  symbol?: string;
  timeframe?: string;
  params: Record<string, ParamValue>;
};

/**
 * Parse a strategy-library handoff from URL query parameters.
 *
 * Returns null when no `strategy` is present, so callers can tell "no handoff"
 * from "handoff with empty params". Malformed JSON degrades to empty params
 * rather than throwing: a bad link should open the page on defaults, not break it.
 */
export function parseStrategyHandoff(
  searchParams: Pick<URLSearchParams, "get">,
  allowedTimeframes: readonly string[],
): StrategyHandoff | null {
  const strategy = searchParams.get("strategy");
  if (!strategy) return null;

  let params: Record<string, ParamValue> = {};
  const raw = searchParams.get("params");
  if (raw) {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        params = parsed as Record<string, ParamValue>;
      }
    } catch {
      params = {};
    }
  }

  const timeframe = searchParams.get("timeframe");
  return {
    strategy,
    symbol: searchParams.get("symbol") ?? undefined,
    // An unsupported timeframe must not reach the select: it would leave the
    // control showing a value that is not among its options.
    timeframe:
      timeframe && allowedTimeframes.includes(timeframe) ? timeframe : undefined,
    params,
  };
}
