import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/api";
import type { ScreenFilter, ScreenResult } from "../../shared/api";
import { pct, price } from "../../shared/format";
import { useToast } from "../../shared/toast";
import { ButtonField } from "../../components/form";
import { Badge, Card, EmptyState, Field, Loader, Stat } from "../../components/ui";

const BOARDS = [
  { value: "main", label: "主板" },
  { value: "gem", label: "创业板" },
  { value: "star", label: "科创板" },
  { value: "bse", label: "北交所" },
];

const SORTS = [
  { value: "change_pct", label: "涨跌幅" },
  { value: "amount", label: "成交额" },
  { value: "turnover", label: "换手率" },
  { value: "pe", label: "市盈率" },
  { value: "pb", label: "市净率" },
  { value: "market_cap", label: "总市值" },
  { value: "price", label: "价格" },
];

/**
 * Numeric filter inputs are held as strings so a half-typed "1." does not become
 * NaN and silently clear the filter. Empty means "no bound".
 */
type NumState = Record<string, string>;

const NUMERIC_FIELDS: { key: keyof ScreenFilter; label: string; hint?: string }[] = [
  { key: "price_min", label: "价格 ≥" },
  { key: "price_max", label: "价格 ≤" },
  { key: "change_pct_min", label: "涨跌幅 ≥ %", hint: "可为负" },
  { key: "change_pct_max", label: "涨跌幅 ≤ %" },
  { key: "amount_min", label: "成交额 ≥ 亿" },
  { key: "turnover_min", label: "换手率 ≥ %" },
  { key: "turnover_max", label: "换手率 ≤ %" },
  { key: "pe_min", label: "PE ≥" },
  { key: "pe_max", label: "PE ≤" },
  { key: "pb_min", label: "PB ≥" },
  { key: "pb_max", label: "PB ≤" },
  { key: "market_cap_min", label: "总市值 ≥ 亿" },
  { key: "market_cap_max", label: "总市值 ≤ 亿", },
];

function toFilter(nums: NumState, boards: string[], excludeSt: boolean, sortBy: string, ascending: boolean, withTechnicals: boolean): ScreenFilter {
  const filter: ScreenFilter = {
    exclude_st: excludeSt,
    boards,
    sort_by: sortBy,
    ascending,
    limit: 50,
    with_technicals: withTechnicals,
  };
  for (const [key, raw] of Object.entries(nums)) {
    const text = raw.trim();
    if (!text) continue;
    const value = Number(text);
    // A non-numeric entry is ignored rather than sent as NaN, which the server
    // would reject as a 422 with a message about JSON parsing.
    if (!Number.isFinite(value)) continue;
    (filter as Record<string, unknown>)[key] = value;
  }
  return filter;
}

export function ScreenerPage() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [nums, setNums] = useState<NumState>({});
  const [boards, setBoards] = useState<string[]>([]);
  const [excludeSt, setExcludeSt] = useState(true);
  const [sortBy, setSortBy] = useState("change_pct");
  const [ascending, setAscending] = useState(false);
  const [withTechnicals, setWithTechnicals] = useState(false);
  const [result, setResult] = useState<ScreenResult | null>(null);

  const presets = useQuery({ queryKey: ["screen-presets"], queryFn: api.screenPresets, staleTime: 600_000 });
  const source = useQuery({
    queryKey: ["screener-source"],
    queryFn: api.screenerSource,
    refetchInterval: 60_000,
  });

  const run = useMutation({
    mutationFn: (filter: ScreenFilter) => api.screen(filter, true),
    onSuccess: (data) => {
      setResult(data);
      if (data.matched === 0) toast.push("没有标的同时满足这些条件，试着放宽阈值", "error");
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const runPreset = useMutation({
    mutationFn: (key: string) => api.screenPreset(key, true),
    onSuccess: (data) => setResult(data),
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const addSymbol = useMutation({
    mutationFn: ({ code, name }: { code: string; name: string }) => api.addScreenedSymbol(code, name),
    onSuccess: (data) => {
      // Report the outcome, not just "added": a symbol registered without bars
      // cannot be backtested, and saying only "added" hides that.
      toast.push(data.detail || `${data.symbol} 已加入标的列表`);
      queryClient.invalidateQueries({ queryKey: ["cn-symbols"] });
      queryClient.invalidateQueries({ queryKey: ["symbols"] });
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const setNum = (key: string, value: string) =>
    setNums((prev) => ({ ...prev, [key]: value }));

  const activeFilter = useMemo(
    () => toFilter(nums, boards, excludeSt, sortBy, ascending, withTechnicals),
    [nums, boards, excludeSt, sortBy, ascending, withTechnicals],
  );
  const pending = run.isPending || runPreset.isPending;

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid grid-4">
        <Stat
          label="全市场标的"
          value={result ? String(result.total_universe) : "—"}
          meta={<span className="muted">单次快照，缓存 60 秒</span>}
        />
        <Stat
          label="命中"
          value={result ? String(result.matched) : "—"}
          accent={result && result.matched > 0 ? "positive" : "neutral"}
          meta={<span className="muted">满足全部条件</span>}
        />
        <Stat
          label="返回"
          value={result ? String(result.returned) : "—"}
          meta={<span className="muted">按排序取前 50</span>}
        />
        <Stat
          label="数据源"
          value={source.data?.available ? "可用" : "不可用"}
          accent={source.data?.available ? "positive" : "negative"}
          meta={
            <span className="muted">
              {source.data?.available
                ? `新浪行情 · ${source.data.latency_ms.toFixed(0)}ms`
                : source.data?.detail ?? "检查网络"}
            </span>
          }
        />
      </div>

      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 380px) minmax(0, 1fr)", alignItems: "start" }}>
        <Card title="筛选条件" subtitle="留空表示不限">
          <div className="stack" style={{ gap: 12 }}>
            <Field label="预设方案" hint="套用后仍可再改条件">
              <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                {(presets.data ?? []).map((preset) => (
                  <ButtonField
                    key={preset.key}
                    size="small"
                    variant="default"
                    disabled={pending}
                    onClick={() => runPreset.mutate(preset.key)}
                    title={preset.description}
                  >
                    {preset.name}
                  </ButtonField>
                ))}
              </div>
            </Field>

            <Field label="板块" hint="不选表示全部">
              <div className="row" style={{ flexWrap: "wrap", gap: 10 }}>
                {BOARDS.map((board) => (
                  <label key={board.value} className="row" style={{ gap: 5, fontSize: 12.5 }}>
                    <input
                      type="checkbox"
                      checked={boards.includes(board.value)}
                      onChange={(event) =>
                        setBoards((prev) =>
                          event.target.checked
                            ? [...prev, board.value]
                            : prev.filter((item) => item !== board.value),
                        )
                      }
                    />
                    {board.label}
                  </label>
                ))}
              </div>
            </Field>

            <div className="grid grid-2" style={{ gap: 10 }}>
              {NUMERIC_FIELDS.map((field) => (
                <label key={field.key} className="stack" style={{ gap: 4 }}>
                  <span className="helper">{field.label}</span>
                  <input
                    className="input"
                    inputMode="decimal"
                    placeholder={field.hint ?? "不限"}
                    value={nums[field.key as string] ?? ""}
                    onChange={(event) => setNum(field.key as string, event.target.value)}
                  />
                </label>
              ))}
            </div>

            <Field label="排序">
              <div className="row" style={{ gap: 8 }}>
                <select
                  className="select"
                  value={sortBy}
                  onChange={(event) => setSortBy(event.target.value)}
                >
                  {SORTS.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
                <select
                  className="select"
                  style={{ width: 100 }}
                  value={ascending ? "asc" : "desc"}
                  onChange={(event) => setAscending(event.target.value === "asc")}
                >
                  <option value="desc">降序</option>
                  <option value="asc">升序</option>
                </select>
              </div>
            </Field>

            <label className="row" style={{ gap: 6, fontSize: 12.5 }}>
              <input type="checkbox" checked={excludeSt} onChange={(e) => setExcludeSt(e.target.checked)} />
              排除 ST / *ST
            </label>
            <label className="row" style={{ gap: 6, fontSize: 12.5 }}>
              <input
                type="checkbox"
                checked={withTechnicals}
                onChange={(e) => setWithTechnicals(e.target.checked)}
              />
              附带技术指标（需本地有 K 线）
            </label>

            <ButtonField disabled={pending} onClick={() => run.mutate(activeFilter)}>
              {pending ? "筛选中…" : "开始筛选"}
            </ButtonField>
            {pending ? (
              <div className="helper">首次拉取全市场约需 8–12 秒，之后 60 秒内复用快照。</div>
            ) : null}
          </div>
        </Card>

        <Card
          title="候选标的"
          subtitle={result ? `命中 ${result.matched} 只，显示 ${result.returned} 只` : "运行一次筛选查看结果"}
          padded={false}
        >
          {run.isPending || runPreset.isPending ? (
            <Loader />
          ) : !result ? (
            <EmptyState
              title="还没有结果"
              text="选一个预设方案，或填写条件后点击「开始筛选」。"
            />
          ) : result.candidates.length === 0 ? (
            <EmptyState title="没有命中" text="条件过严，试着放宽成交额或涨跌幅。" />
          ) : (
            <div className="table-wrap" style={{ maxHeight: 640 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>代码</th>
                    <th>名称</th>
                    <th>板块</th>
                    <th className="numeric">现价</th>
                    <th className="numeric">涨跌</th>
                    <th className="numeric">成交额</th>
                    <th className="numeric">换手</th>
                    <th className="numeric">PE</th>
                    <th className="numeric">PB</th>
                    <th className="numeric">市值</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {result.candidates.map((item) => (
                    <tr key={item.code}>
                      <td className="mono" style={{ fontWeight: 600 }}>{item.code}</td>
                      <td>
                        {item.name}
                        {item.is_st ? <Badge tone="amber">ST</Badge> : null}
                      </td>
                      <td>
                        <Badge tone="indigo">
                          {BOARDS.find((b) => b.value === item.board)?.label ?? item.board}
                        </Badge>
                      </td>
                      <td className="numeric">{price(item.price)}</td>
                      <td className={`numeric ${item.change_pct >= 0 ? "up" : "down"}`}>
                        {pct(item.change_pct)}
                      </td>
                      <td className="numeric">{item.amount_yi.toFixed(1)} 亿</td>
                      <td className="numeric">{item.turnover_pct.toFixed(1)}%</td>
                      <td className="numeric">{item.pe === null ? "—" : item.pe.toFixed(1)}</td>
                      <td className="numeric">{item.pb === null ? "—" : item.pb.toFixed(2)}</td>
                      <td className="numeric">{item.market_cap_yi.toFixed(0)} 亿</td>
                      <td>
                        <ButtonField
                          size="small"
                          variant="default"
                          disabled={addSymbol.isPending}
                          onClick={() => addSymbol.mutate({ code: item.code, name: item.name })}
                        >
                          {addSymbol.isPending &&
                          addSymbol.variables?.code === item.code
                            ? "导入中…"
                            : "加入并可回测"}
                        </ButtonField>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {result && result.warnings.length > 0 ? (
        <Card title="提示">
          <div className="stack" style={{ gap: 6 }}>
            {result.warnings.map((warning) => (
              <div key={warning} className="helper">{warning}</div>
            ))}
          </div>
        </Card>
      ) : null}
    </div>
  );
}
