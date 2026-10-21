import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/api";
import { timeLabel } from "../../shared/format";
import { Badge, Card, EmptyState, Field, Loader } from "../../components/ui";
import { ButtonField } from "../../components/form";
import { useToast } from "../../shared/toast";

export function DataPage() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [replace, setReplace] = useState(true);
  const [timeframe, setTimeframe] = useState("15m");
  const coverage = useQuery({ queryKey: ["data-coverage"], queryFn: api.dataCoverage, refetchInterval: 20_000 });
  const symbols = useQuery({ queryKey: ["symbols", "all"], queryFn: () => api.symbols() });

  // The coverage table has one row per (symbol, timeframe); a plain map over it
  // listed every symbol twice in the picker.
  const uniqueSymbols = useMemo(
    () => [...new Set((coverage.data ?? []).map((item) => item.symbol))],
    [coverage.data],
  );

  const symbolMeta = useMemo(
    () => (symbols.data ?? []).find((item) => item.code === symbol) ?? null,
    [symbols.data, symbol],
  );

  // The backend accepts the symbol's base timeframe, plus native daily bars for
  // A-shares (what real exchange exports contain). Offering anything else would
  // just produce a 422 after the upload.
  const importTimeframes = useMemo(() => {
    const base = symbolMeta?.base_timeframe || "15m";
    return symbolMeta?.market === "cn_stock" ? [base, "1d"] : [base];
  }, [symbolMeta]);

  useEffect(() => {
    if (!importTimeframes.includes(timeframe)) setTimeframe(importTimeframes[0]);
  }, [importTimeframes, timeframe]);

  const quality = useQuery({
    queryKey: ["data-quality", symbol, timeframe],
    queryFn: () => api.dataQuality(symbol, timeframe),
    enabled: Boolean(symbol),
  });

  const upload = useMutation({
    mutationFn: (file: File) => api.importCsv(symbol, file, replace, timeframe),
    onSuccess: (report) => {
      toast.push(`导入完成：新增 ${report.imported} 行，跳过 ${report.skipped} 行`);
      queryClient.invalidateQueries({ queryKey: ["data-quality"] });
      queryClient.invalidateQueries({ queryKey: ["data-coverage"] });
      queryClient.invalidateQueries({ queryKey: ["candles"] });
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1.4fr) minmax(0, 1fr)" }}>
        <Card title="导入 K 线" subtitle="CSV：open_time,open,high,low,close[,volume]">
          <div className="stack" style={{ gap: 14 }}>
            <div className="grid grid-2" style={{ gap: 12 }}>
              <Field label="标的">
                <select className="select" value={symbol} onChange={(event) => setSymbol(event.target.value)}>
                  {uniqueSymbols.map((code) => (
                    <option key={code} value={code}>
                      {code}
                    </option>
                  ))}
                  {uniqueSymbols.length === 0 || !uniqueSymbols.includes(symbol) ? (
                    <option value={symbol}>{symbol}</option>
                  ) : null}
                </select>
              </Field>
              <Field
                label="周期"
                hint={symbolMeta?.market === "cn_stock" ? "A 股可用原生日线" : "加密只接受基础周期"}
              >
                <select
                  className="select"
                  value={timeframe}
                  onChange={(event) => setTimeframe(event.target.value)}
                >
                  {importTimeframes.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="写入方式">
                <select className="select" value={replace ? "replace" : "append"} onChange={(event) => setReplace(event.target.value === "replace")}>
                  <option value="replace">覆盖该标的历史</option>
                  <option value="append">追加（去重）</option>
                </select>
              </Field>
            </div>
            <div
              style={{
                border: "1px dashed var(--border-strong)",
                borderRadius: 12,
                padding: 22,
                textAlign: "center",
                background: "var(--surface-subtle)",
              }}
            >
              <div style={{ fontWeight: 600, fontSize: 13 }}>选择 CSV 文件（最大 20 MB，UTF-8）</div>
              <div className="helper" style={{ margin: "4px 0 12px" }}>
                时间戳为毫秒；非法 OHLC 行会被跳过并报告。
              </div>
              <input
                ref={fileRef}
                type="file"
                accept=".csv,text/csv"
                style={{ display: "none" }}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) upload.mutate(file);
                  event.target.value = "";
                }}
              />
              <ButtonField onClick={() => fileRef.current?.click()} disabled={upload.isPending}>
                {upload.isPending ? "导入中…" : "选择文件并导入"}
              </ButtonField>
            </div>
            {upload.data ? (
              <div className="stack" style={{ gap: 6 }}>
                <div className="row" style={{ gap: 10 }}>
                  <Badge tone="green">新增 {upload.data.imported}</Badge>
                  <Badge tone={upload.data.skipped ? "amber" : ""}>跳过 {upload.data.skipped}</Badge>
                </div>
                {upload.data.errors.map((message) => (
                  <div key={message} className="error-text">{message}</div>
                ))}
              </div>
            ) : null}
          </div>
        </Card>

        <Card title="数据质量" subtitle={`${symbol} · ${timeframe}`}>
          {quality.isLoading || !quality.data ? (
            <Loader />
          ) : (
            <div className="stack" style={{ gap: 9 }}>
              <Row label="K 线数量" value={String(quality.data.bars)} />
              <Row label="首根时间" value={quality.data.first_time ? timeLabel(quality.data.first_time) : "—"} />
              <Row label="末根时间" value={quality.data.last_time ? timeLabel(quality.data.last_time) : "—"} />
              <Row label="重复时间戳" value={String(quality.data.duplicate_times)} tone={quality.data.duplicate_times ? "down" : undefined} />
              <Row label="非单调" value={String(quality.data.non_monotonic)} tone={quality.data.non_monotonic ? "down" : undefined} />
              <Row label="非法 OHLC" value={String(quality.data.invalid_ohlc)} tone={quality.data.invalid_ohlc ? "down" : undefined} />
              <Row label="缺口" value={String(quality.data.gaps)} tone={quality.data.gaps ? "down" : undefined} />
              <div className="helper">
                {timeframe === "1d"
                  ? "原生日线序列，直接由数据源提供。"
                  : "基础周期；其他周期由读取时聚合。"}
              </div>
            </div>
          )}
        </Card>
      </div>

      <Card title="数据覆盖" subtitle="各标的基础 K 线范围" padded={false}>
        {coverage.isLoading ? (
          <Loader />
        ) : coverage.data && coverage.data.length > 0 ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>标的</th>
                  <th>周期</th>
                  <th className="numeric">数量</th>
                  <th>起始</th>
                  <th>结束</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {coverage.data.map((item) => (
                  <tr key={`${item.symbol}-${item.timeframe}`}>
                    <td style={{ fontWeight: 600 }}>{item.symbol}</td>
                    <td className="muted">{item.timeframe}</td>
                    <td className="numeric">{item.bars.toLocaleString("en-US")}</td>
                    <td className="mono muted">{timeLabel(item.first_time)}</td>
                    <td className="mono muted">{timeLabel(item.last_time)}</td>
                    <td>
                      <a className="button small ghost" href={`/api/data/export?symbol=${item.symbol}&timeframe=${item.timeframe}&limit=20000`}>
                        导出 CSV
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="暂无数据" text="行情引擎首次启动时会自动生成历史，也可以用 CSV 导入。" />
        )}
      </Card>
    </div>
  );
}

function Row({ label, value, tone }: { label: string; value: string; tone?: "down" }) {
  return (
    <div className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
      <span className="muted">{label}</span>
      <span className={`num ${tone ?? ""}`} style={{ fontWeight: 550 }}>{value}</span>
    </div>
  );
}
