import { describe, expect, it } from "vitest";
import { parseStrategyHandoff } from "./handoff";

const TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"] as const;

const params = (query: string) => new URLSearchParams(query);

describe("parseStrategyHandoff", () => {
  it("returns null without a strategy", () => {
    expect(parseStrategyHandoff(params(""), TIMEFRAMES)).toBeNull();
    expect(parseStrategyHandoff(params("symbol=BTCUSDT"), TIMEFRAMES)).toBeNull();
  });

  it("reads every field the strategy library sends", () => {
    const handoff = parseStrategyHandoff(
      params(
        "strategy=sma_cross&symbol=600036.SH&timeframe=1d&params=%7B%22fast%22%3A7%2C%22slow%22%3A55%2C%22require_regime%22%3Atrue%7D",
      ),
      TIMEFRAMES,
    );
    expect(handoff).toEqual({
      strategy: "sma_cross",
      symbol: "600036.SH",
      timeframe: "1d",
      params: { fast: 7, slow: 55, require_regime: true },
    });
  });

  it("preserves parameter types through the JSON round trip", () => {
    // Regression: a boolean arriving as 0/1 made the checkbox look unchecked and
    // failed backend schema validation.
    const handoff = parseStrategyHandoff(
      params('strategy=x&params={"enabled":false,"period":14,"label":"a"}'),
      TIMEFRAMES,
    );
    expect(handoff?.params.enabled).toBe(false);
    expect(handoff?.params.period).toBe(14);
  });

  it("keeps every field optional except the strategy", () => {
    const handoff = parseStrategyHandoff(params("strategy=rsi_reversion"), TIMEFRAMES);
    expect(handoff).toEqual({
      strategy: "rsi_reversion",
      symbol: undefined,
      timeframe: undefined,
      params: {},
    });
  });

  it("drops an unsupported timeframe instead of feeding it to the select", () => {
    const handoff = parseStrategyHandoff(params("strategy=x&timeframe=3m"), TIMEFRAMES);
    expect(handoff?.timeframe).toBeUndefined();
  });

  it("survives malformed JSON by falling back to empty params", () => {
    // A truncated link should open the page on defaults, not crash it.
    expect(
      parseStrategyHandoff(params("strategy=x&params=%7Bfast%3A7"), TIMEFRAMES)?.params,
    ).toEqual({});
  });

  it("rejects JSON that is not an object", () => {
    expect(parseStrategyHandoff(params("strategy=x&params=[1,2]"), TIMEFRAMES)?.params).toEqual({});
    expect(parseStrategyHandoff(params("strategy=x&params=5"), TIMEFRAMES)?.params).toEqual({});
    expect(parseStrategyHandoff(params("strategy=x&params=null"), TIMEFRAMES)?.params).toEqual({});
  });

  it("ignores an empty params value", () => {
    expect(parseStrategyHandoff(params("strategy=x&params="), TIMEFRAMES)?.params).toEqual({});
  });
});
