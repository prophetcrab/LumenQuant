import type { StrategyMeta } from "./api";

export type ParamValue = string | number | boolean;

type PropertySchema = NonNullable<StrategyMeta["params_schema"]["properties"]>[string];

/** Coerce a form value back to the type declared by the strategy schema. */
export function coerceParam(schema: PropertySchema | undefined, raw: ParamValue): ParamValue {
  const type = schema?.type;
  if (type === "boolean") return Boolean(raw);
  if (type === "integer" || type === "number") {
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return raw;
}

/** Build the parameter payload for a strategy from form strings. */
export function buildParams(
  meta: StrategyMeta | null | undefined,
  form: Record<string, ParamValue>,
): Record<string, ParamValue> {
  if (!meta) return {};
  const props = meta.params_schema.properties ?? {};
  const out: Record<string, ParamValue> = {};
  Object.entries(form).forEach(([key, value]) => {
    out[key] = coerceParam(props[key], value);
  });
  return out;
}

/** Initial form state: schema defaults, keeping booleans as booleans. */
export function initialParams(meta: StrategyMeta | null | undefined): Record<string, ParamValue> {
  if (!meta) return {};
  const props = meta.params_schema.properties ?? {};
  const out: Record<string, ParamValue> = {};
  Object.entries(props).forEach(([key, schema]) => {
    const fallback = meta.defaults[key];
    const typed = schema as { type?: string; default?: number | string | boolean };
    if (typed.type === "boolean") {
      out[key] = typed.default === true || fallback === true;
    } else {
      out[key] = String(typed.default ?? fallback ?? "");
    }
  });
  return out;
}
