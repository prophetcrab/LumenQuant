/** Supported bar timeframes per market, mirroring the backend's tables.

A-share data has no 4h series: the market closes mid-day, so a 4h bar is not a
meaningful unit and the backend rejects it. Offering 4h in an A-share picker
produced a 422 only after the user had configured everything else.
 */
export const CRYPTO_TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"] as const;
export const CN_TIMEFRAMES = ["15m", "30m", "1h", "1d"] as const;

export type Timeframe = (typeof CRYPTO_TIMEFRAMES)[number];

export function timeframesFor(market: "crypto" | "cn_stock" | undefined | null): readonly Timeframe[] {
  return market === "cn_stock" ? CN_TIMEFRAMES : CRYPTO_TIMEFRAMES;
}
