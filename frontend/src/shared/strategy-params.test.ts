import { describe, expect, it } from "vitest";
import { buildParams, coerceParam, initialParams } from "./strategy-params";
import type { StrategyMeta } from "./api";

const meta = {
  key: "sma_cross",
  name: "双均线交叉",
  category: "trend",
  description: "",
  tags: [],
  defaults: { fast: 10, slow: 30, require_regime: false },
  params_schema: {
    properties: {
      fast: { type: "integer", title: "快线周期", default: 10, minimum: 2, maximum: 200 },
      slow: { type: "integer", title: "慢线周期", default: 30, minimum: 3, maximum: 400 },
      require_regime: { type: "boolean", title: "牛熊过滤", default: false },
      note: { type: "string", title: "备注", default: "" },
    },
  },
} as unknown as StrategyMeta;

const props = meta.params_schema.properties!;

describe("coerceParam", () => {
  it("keeps booleans as booleans", () => {
    // Regression: Number(false) === 0, so an unchecked box used to arrive at the
    // backend as the integer 0 and fail schema validation.
    expect(coerceParam(props.require_regime, false)).toBe(false);
    expect(coerceParam(props.require_regime, true)).toBe(true);
  });

  it("turns numeric strings into numbers", () => {
    expect(coerceParam(props.fast, "12")).toBe(12);
    expect(coerceParam(props.fast, "12.7")).toBe(12.7);
  });

  it("falls back to 0 for unparseable numbers instead of NaN", () => {
    expect(coerceParam(props.fast, "")).toBe(0);
    expect(coerceParam(props.fast, "abc")).toBe(0);
  });

  it("leaves string params untouched", () => {
    expect(coerceParam(props.note, "hello")).toBe("hello");
  });
});

describe("initialParams", () => {
  it("renders numbers as form-friendly strings", () => {
    const values = initialParams(meta);
    expect(values.fast).toBe("10");
    expect(values.slow).toBe("30");
  });

  it("keeps booleans typed so the checkbox renders checked", () => {
    expect(initialParams(meta).require_regime).toBe(false);
    const enabled = {
      ...meta,
      defaults: { ...meta.defaults, require_regime: true },
    } as unknown as StrategyMeta;
    expect(initialParams(enabled).require_regime).toBe(true);
  });

  it("returns an empty object without a catalog entry", () => {
    expect(initialParams(null)).toEqual({});
  });
});

describe("buildParams", () => {
  it("coerces the whole form to schema types", () => {
    const payload = buildParams(meta, {
      fast: "7",
      slow: "55",
      require_regime: true,
    });
    expect(payload).toEqual({ fast: 7, slow: 55, require_regime: true });
  });

  it("does not invent values for fields absent from the form", () => {
    // The handoff path merges schema defaults into state before this runs; adding
    // defaults here as well would silently override a deliberate omission.
    expect(buildParams(meta, { fast: "5" })).toEqual({ fast: 5 });
  });

  it("returns an empty payload without a catalog entry", () => {
    expect(buildParams(undefined, { fast: "5" })).toEqual({});
  });
});
