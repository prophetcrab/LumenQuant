import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/api";
import type { StrategyInstance, StrategyMeta } from "../../shared/api";
import { useAccount } from "../../shared/account-context";
import { useToast } from "../../shared/toast";
import { dateTimeLabel, timeLabel, timeframeLabel } from "../../shared/format";
import { Badge, Card, EmptyState, Field, Loader, Modal, Stat } from "../../components/ui";
import { ButtonField } from "../../components/form";
import { buildParams, initialParams, type ParamValue } from "../../shared/strategy-params";
import { parseStrategyHandoff } from "../../shared/handoff";

const TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"] as const;

export function RuntimePage() {
  const { account, accountId } = useAccount();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  // Handoff from the strategy library ("建成自动实例"). Read once at mount so
  // the creation dialog opens on the tuned configuration.
  const handoff = useRef(parseStrategyHandoff(searchParams, TIMEFRAMES));
  const [createOpen, setCreateOpen] = useState(handoff.current !== null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [strategyKey, setStrategyKey] = useState(handoff.current?.strategy ?? "sma_cross");
  const [symbol, setSymbol] = useState(handoff.current?.symbol ?? "BTCUSDT");
  const [timeframe, setTimeframe] = useState<(typeof TIMEFRAMES)[number]>(
    (handoff.current?.timeframe as (typeof TIMEFRAMES)[number] | undefined) ?? "1h",
  );
  const [positionPct, setPositionPct] = useState("20");
  const [maxNotional, setMaxNotional] = useState("20000");
  const [stopLoss, setStopLoss] = useState("0");
  const [takeProfit, setTakeProfit] = useState("0");
  const [trailingStop, setTrailingStop] = useState("0");
  const [params, setParams] = useState<Record<string, ParamValue>>(
    handoff.current?.params ?? {},
  );
  // Editing an existing instance: the backend accepts param/risk changes, but
  // strategy, symbol and timeframe stay fixed so the running history keeps meaning.
  const [editing, setEditing] = useState<StrategyInstance | null>(null);
  const [editParams, setEditParams] = useState<Record<string, ParamValue>>({});
  const [editPositionPct, setEditPositionPct] = useState("0");
  const [editMaxNotional, setEditMaxNotional] = useState("0");
  const [editStopLoss, setEditStopLoss] = useState("0");
  const [editTakeProfit, setEditTakeProfit] = useState("0");
  const [editTrailingStop, setEditTrailingStop] = useState("0");

  const catalog = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });
  const symbols = useQuery({ queryKey: ["symbols", "all"], queryFn: () => api.symbols() });
  const cnSymbols = useQuery({
    queryKey: ["cn-symbols"],
    queryFn: api.cnSymbols,
    enabled: account?.market === "cn_stock",
    refetchInterval: 15_000,
  });
  const instances = useQuery({
    queryKey: ["runtime-instances", accountId],
    queryFn: () => api.runtimeInstances(accountId),
    enabled: Boolean(accountId),
    refetchInterval: 5_000,
  });
  const logs = useQuery({
    queryKey: ["runtime-logs", selectedId],
    queryFn: () => api.runtimeLogs(selectedId as string, 100),
    enabled: Boolean(selectedId),
    refetchInterval: 4_000,
  });

  const meta: StrategyMeta | undefined = catalog.data?.find((item) => item.key === strategyKey);

  const metaKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!meta) return;
    // Merge schema defaults without clobbering handed-over values; reset only
    // when the user actually switches strategy.
    const switched = metaKeyRef.current !== null && metaKeyRef.current !== meta.key;
    metaKeyRef.current = meta.key;
    setParams((prev) =>
      switched ? initialParams(meta) : { ...initialParams(meta), ...prev },
    );
  }, [meta]);

  useEffect(() => {
    if (!handoff.current) return;
    handoff.current = null;
    setSearchParams({}, { replace: true });
  }, [setSearchParams]);

  useEffect(() => {
    if (!selectedId && instances.data && instances.data.length > 0) {
      setSelectedId(instances.data[0].id);
    }
  }, [instances.data, selectedId]);

  const create = useMutation({
    mutationFn: () => {
      const parsed = buildParams(meta, params);
      return api.createRuntimeInstance({
        account_id: accountId as string,
        strategy_key: strategyKey,
        symbol,
        timeframe,
        params: parsed,
        position_pct: Number(positionPct) / 100,
        max_order_notional: Number(maxNotional),
        stop_loss_pct: Number(stopLoss),
        take_profit_pct: Number(takeProfit),
        trailing_stop_pct: Number(trailingStop),
        start: true,
      });
    },
    onSuccess: (instance) => {
      toast.push(`实例「${instance.strategy_name}」已启动`);
      setCreateOpen(false);
      setSelectedId(instance.id);
      queryClient.invalidateQueries({ queryKey: ["runtime-instances"] });
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const toggle = useMutation({
    mutationFn: ({ id, running }: { id: string; running: boolean }) =>
      api.toggleRuntimeInstance(id, running),
    onSuccess: (instance) => {
      toast.push(instance.running ? "实例已启动" : "实例已停止");
      queryClient.invalidateQueries({ queryKey: ["runtime-instances"] });
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const openEdit = (instance: StrategyInstance) => {
    setEditing(instance);
    const editMeta = catalog.data?.find((item) => item.key === instance.strategy_key);
    // Schema defaults cover params added after the instance was created.
    setEditParams({ ...initialParams(editMeta), ...instance.params });
    setEditPositionPct(String(Math.round(instance.position_pct * 100)));
    setEditMaxNotional(String(instance.max_order_notional));
    setEditStopLoss(String(+(instance.stop_loss_pct * 100).toFixed(4)));
    setEditTakeProfit(String(+(instance.take_profit_pct * 100).toFixed(4)));
    setEditTrailingStop(String(+(instance.trailing_stop_pct * 100).toFixed(4)));
  };

  const saveEdit = useMutation({
    mutationFn: () => {
      if (!editing) throw new Error("没有待编辑的实例");
      const editMeta = catalog.data?.find((item) => item.key === editing.strategy_key);
      return api.updateRuntimeInstance(editing.id, {
        params: buildParams(editMeta, editParams),
        position_pct: Number(editPositionPct) / 100,
        max_order_notional: Number(editMaxNotional),
        stop_loss_pct: Number(editStopLoss),
        take_profit_pct: Number(editTakeProfit),
        trailing_stop_pct: Number(editTrailingStop),
      });
    },
    onSuccess: () => {
      toast.push("实例配置已更新");
      setEditing(null);
      queryClient.invalidateQueries({ queryKey: ["runtime-instances"] });
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteRuntimeInstance(id),
    onSuccess: () => {
      toast.push("实例已删除");
      setSelectedId(null);
      queryClient.invalidateQueries({ queryKey: ["runtime-instances"] });
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  const tick = useMutation({
    mutationFn: api.runtimeTick,
    onSuccess: (result) => {
      toast.push(`已评估 ${Object.keys(result.evaluated).length} 个运行中实例`);
      queryClient.invalidateQueries({ queryKey: ["runtime-instances"] });
      queryClient.invalidateQueries({ queryKey: ["runtime-logs"] });
      queryClient.invalidateQueries();
    },
    onError: (error: Error) => toast.push(error.message, "error"),
  });

  // An instance can only trade the account's own market; offering the other
  // market in the picker produces a 422 only after the user fills the dialog.
  const marketSymbols = useMemo(
    () => (symbols.data ?? []).filter((item) => !account || item.market === account.market),
    [symbols.data, account],
  );

  useEffect(() => {
    if (marketSymbols.length === 0) return;
    if (!marketSymbols.some((item) => item.code === symbol)) {
      setSymbol(marketSymbols[0].code);
    }
  }, [marketSymbols, symbol]);

  const stats = useMemo(() => {
    const list = instances.data ?? [];
    return {
      total: list.length,
      running: list.filter((item) => item.running).length,
      executions: list.reduce((sum, item) => sum + item.executions, 0),
      errors: list.filter((item) => item.last_error).length,
    };
  }, [instances.data]);

  const selected = instances.data?.find((item) => item.id === selectedId) ?? null;
  // A-share sizing guard: warn before the user creates an instance whose notional
  // cap cannot cover a single board lot for the chosen symbol.
  const cnLotCost = useMemo(() => {
    if (account?.market !== "cn_stock") return 0;
    const row = cnSymbols.data?.find((item) => item.code === symbol);
    return row ? row.price * 100 : 0;
  }, [account?.market, cnSymbols.data, symbol]);
  const capTooSmall =
    cnLotCost > 0 && Number(maxNotional) > 0 && Number(maxNotional) < cnLotCost;

  if (!account) {
    return <EmptyState title="请先创建账户" text="自动运行需要一个模拟账户来接收订单。" />;
  }

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid grid-4">
        <Stat label="实例总数" value={String(stats.total)} meta={<span className="muted">当前账户</span>} />
        <Stat label="运行中" value={String(stats.running)} accent="positive" meta={<span className="muted">后台调度器每 6 秒评估</span>} />
        <Stat label="累计执行" value={String(stats.executions)} meta={<span className="muted">开仓与平仓次数</span>} />
        <Stat
          label="异常实例"
          value={String(stats.errors)}
          accent={stats.errors > 0 ? "negative" : "neutral"}
          meta={<span className="muted">检查日志中的错误</span>}
        />
      </div>

      <Card
        title="自动运行实例"
        subtitle="信号来自最新已收盘 K 线；同一根 K 线只处理一次"
        actions={
          <>
            <ButtonField variant="default" size="small" onClick={() => tick.mutate()} disabled={tick.isPending}>
              {tick.isPending ? "评估中…" : "立即评估"}
            </ButtonField>
            <ButtonField size="small" onClick={() => setCreateOpen(true)}>
              新建实例
            </ButtonField>
          </>
        }
        padded={false}
      >
        {instances.isLoading ? (
          <Loader />
        ) : instances.data && instances.data.length > 0 ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>策略</th>
                  <th>标的 / 周期</th>
                  <th>参数</th>
                  <th className="numeric">单次仓位</th>
                  <th>保护</th>
                  <th className="numeric">执行次数</th>
                  <th>最近信号</th>
                  <th>状态</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {instances.data.map((instance) => (
                  <tr
                    key={instance.id}
                    style={{ cursor: "pointer", background: instance.id === selectedId ? "var(--surface-active)" : undefined }}
                    onClick={() => setSelectedId(instance.id)}
                  >
                    <td style={{ fontWeight: 600 }}>{instance.strategy_name}</td>
                    <td>
                      {instance.symbol}
                      <span className="muted"> · {instance.timeframe}</span>
                    </td>
                    <td className="mono muted" style={{ maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis" }}>
                      {Object.entries(instance.params).map(([k, v]) => `${k}=${v}`).join(" ")}
                    </td>
                    <td className="numeric">{(instance.position_pct * 100).toFixed(0)}%</td>
                    <td className="mono muted" style={{ fontSize: 11 }}>
                      {instance.stop_loss_pct || instance.take_profit_pct || instance.trailing_stop_pct
                        ? [
                            instance.stop_loss_pct ? `止损${(instance.stop_loss_pct * 100).toFixed(0)}%` : "",
                            instance.take_profit_pct ? `止盈${(instance.take_profit_pct * 100).toFixed(0)}%` : "",
                            instance.trailing_stop_pct ? `移动${(instance.trailing_stop_pct * 100).toFixed(0)}%` : "",
                          ]
                            .filter(Boolean)
                            .join(" · ")
                        : "无"}
                    </td>
                    <td className="numeric">{instance.executions}</td>
                    <td className="muted">
                      {instance.last_signal_at ? timeLabel(new Date(instance.last_signal_at).getTime()) : "—"}
                      {instance.last_error ? <div className="error-text">{instance.last_error}</div> : null}
                    </td>
                    <td>
                      <Badge tone={instance.running ? "green" : ""}>{instance.running ? "运行中" : "已停止"}</Badge>
                    </td>
                    <td>
                      <div className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
                        <button
                          className="button small"
                          onClick={(event) => {
                            event.stopPropagation();
                            toggle.mutate({ id: instance.id, running: !instance.running });
                          }}
                        >
                          {instance.running ? "停止" : "启动"}
                        </button>
                        <button
                          className="button small ghost"
                          onClick={(event) => {
                            event.stopPropagation();
                            openEdit(instance);
                          }}
                        >
                          编辑
                        </button>
                        <button
                          className="button small ghost"
                          onClick={(event) => {
                            event.stopPropagation();
                            remove.mutate(instance.id);
                          }}
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title="还没有自动运行实例"
            text="选择一个策略、标的与周期，系统会在每根新 K 线收盘后评估信号并自动下单。"
            action={<button className="button primary" onClick={() => setCreateOpen(true)}>新建实例</button>}
          />
        )}
      </Card>

      <Card
        title={selected ? `${selected.strategy_name} · 运行日志` : "运行日志"}
        subtitle={selected ? `${selected.symbol} · ${timeframeLabel(selected.timeframe)} · 创建于 ${dateTimeLabel(selected.created_at)}` : "点击上方实例查看"}
        padded={false}
      >
        {selected ? (
          logs.isLoading ? (
            <Loader />
          ) : logs.data && logs.data.length > 0 ? (
            <div className="table-wrap" style={{ maxHeight: 380 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>级别</th>
                    <th>事件</th>
                    <th>信息</th>
                    <th>关联订单</th>
                  </tr>
                </thead>
                <tbody>
                  {logs.data.map((log) => (
                    <tr key={log.id}>
                      <td className="mono muted">{dateTimeLabel(log.created_at)}</td>
                      <td>
                        <Badge tone={log.level === "error" ? "red" : log.level === "warn" ? "amber" : "green"}>
                          {log.level}
                        </Badge>
                      </td>
                      <td className="muted">{log.event}</td>
                      <td>{log.message}</td>
                      <td className="mono muted">{log.order_id ? `${log.order_id.slice(0, 8)}…` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="暂无日志" />
          )
        ) : (
          <EmptyState title="选择实例查看日志" />
        )}
      </Card>

      <Modal
        open={createOpen}
        title="新建自动运行实例"
        onClose={() => setCreateOpen(false)}
        footer={
          <>
            <button className="button" onClick={() => setCreateOpen(false)}>
              取消
            </button>
            <button
              className="button primary"
              disabled={create.isPending || capTooSmall}
              onClick={() => create.mutate()}
            >
              {create.isPending ? "创建中…" : "创建并启动"}
            </button>
          </>
        }
      >
        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="策略">
            <select className="select" value={strategyKey} onChange={(event) => setStrategyKey(event.target.value)}>
              {(catalog.data ?? []).map((item) => (
                <option key={item.key} value={item.key}>
                  {item.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="标的" hint={`仅显示 ${account.market === "cn_stock" ? "A 股" : "加密"} 标的`}>
            <select className="select" value={symbol} onChange={(event) => setSymbol(event.target.value)}>
              {marketSymbols.map((item) => (
                <option key={item.code} value={item.code}>
                  {item.code}
                </option>
              ))}
            </select>
          </Field>
          <Field label="周期">
            <select
              className="select"
              value={timeframe}
              onChange={(event) => setTimeframe(event.target.value as (typeof TIMEFRAMES)[number])}
            >
              {TIMEFRAMES.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </Field>
          <Field label="单次仓位 %" hint="按可用现金比例开仓">
            <input className="input num" type="number" value={positionPct} onChange={(event) => setPositionPct(event.target.value)} />
          </Field>
          <Field label="止损 %" hint="0 表示不启用；触发即平仓">
            <input className="input num" type="number" min={0} max={50} value={stopLoss} onChange={(event) => setStopLoss(event.target.value)} />
          </Field>
          <Field label="止盈 %" hint="0 表示不启用">
            <input className="input num" type="number" min={0} max={500} value={takeProfit} onChange={(event) => setTakeProfit(event.target.value)} />
          </Field>
          <Field label="移动止损 %" hint="自峰值回撤该比例离场；0 关闭">
            <input className="input num" type="number" min={0} max={50} value={trailingStop} onChange={(event) => setTrailingStop(event.target.value)} />
          </Field>
          <Field
            label="单笔名义上限"
            hint="风控硬上限"
            error={
              capTooSmall
                ? `低于一手成本 ${cnLotCost.toFixed(2)} 元，实例将无法建仓`
                : undefined
            }
          >
            <input className="input num" type="number" value={maxNotional} onChange={(event) => setMaxNotional(event.target.value)} />
          </Field>
          {Object.entries(meta?.params_schema.properties ?? {}).map(([key, schema]) => (
            <Field key={key} label={schema.title ?? key}>
              {schema.type === "boolean" ? (
                <label className="row" style={{ gap: 8, height: 36 }}>
                  <input
                    type="checkbox"
                    checked={Boolean(params[key])}
                    onChange={(event) =>
                      setParams((prev) => ({ ...prev, [key]: event.target.checked }))
                    }
                  />
                  <span className="helper">启用</span>
                </label>
              ) : (
                <input
                  className="input num"
                  type="number"
                  value={String(params[key] ?? "")}
                  onChange={(event) => setParams((prev) => ({ ...prev, [key]: event.target.value }))}
                />
              )}
            </Field>
          ))}
        </div>
        <div className="helper">实例启动后会在下一根已收盘 K 线触发首次评估，并遵守账户级风控。</div>
      </Modal>

      <Modal
        open={editing !== null}
        title={editing ? `编辑实例 · ${editing.strategy_name}` : "编辑实例"}
        onClose={() => setEditing(null)}
        footer={
          <>
            <button className="button" onClick={() => setEditing(null)}>
              取消
            </button>
            <button
              className="button primary"
              disabled={saveEdit.isPending}
              onClick={() => saveEdit.mutate()}
            >
              {saveEdit.isPending ? "保存中…" : "保存"}
            </button>
          </>
        }
      >
        {editing ? (
          <>
            <div className="grid grid-2" style={{ gap: 12 }}>
              <Field label="策略">
                <input className="input" value={editing.strategy_name} readOnly />
              </Field>
              <Field label="标的 / 周期">
                <input className="input" value={`${editing.symbol} · ${editing.timeframe}`} readOnly />
              </Field>
              <Field label="单次仓位 %" hint="按可用现金比例开仓">
                <input
                  className="input num"
                  type="number"
                  value={editPositionPct}
                  onChange={(event) => setEditPositionPct(event.target.value)}
                />
              </Field>
              <Field label="单笔名义上限" hint="风控硬上限">
                <input
                  className="input num"
                  type="number"
                  value={editMaxNotional}
                  onChange={(event) => setEditMaxNotional(event.target.value)}
                />
              </Field>
              <Field label="止损 %" hint="0 表示不启用；触发即平仓">
                <input
                  className="input num"
                  type="number"
                  min={0}
                  max={50}
                  value={editStopLoss}
                  onChange={(event) => setEditStopLoss(event.target.value)}
                />
              </Field>
              <Field label="止盈 %" hint="0 表示不启用">
                <input
                  className="input num"
                  type="number"
                  min={0}
                  max={500}
                  value={editTakeProfit}
                  onChange={(event) => setEditTakeProfit(event.target.value)}
                />
              </Field>
              <Field label="移动止损 %" hint="自峰值回撤该比例离场；0 关闭">
                <input
                  className="input num"
                  type="number"
                  min={0}
                  max={50}
                  value={editTrailingStop}
                  onChange={(event) => setEditTrailingStop(event.target.value)}
                />
              </Field>
              {Object.entries(
                catalog.data?.find((item) => item.key === editing.strategy_key)?.params_schema
                  .properties ?? {},
              ).map(([key, schema]) => (
                <Field key={key} label={schema.title ?? key}>
                  {schema.type === "boolean" ? (
                    <label className="row" style={{ gap: 8, height: 36 }}>
                      <input
                        type="checkbox"
                        checked={Boolean(editParams[key])}
                        onChange={(event) =>
                          setEditParams((prev) => ({ ...prev, [key]: event.target.checked }))
                        }
                      />
                      <span className="helper">启用</span>
                    </label>
                  ) : (
                    <input
                      className="input num"
                      type="number"
                      value={String(editParams[key] ?? "")}
                      onChange={(event) =>
                        setEditParams((prev) => ({ ...prev, [key]: event.target.value }))
                      }
                    />
                  )}
                </Field>
              ))}
            </div>
            <div className="helper">
              策略、标的与周期不可修改：改了就不再是同一个实例，请停止后新建。
              止损与止盈立即生效；参数改动在下一根已收盘 K 线生效。
            </div>
          </>
        ) : null}
      </Modal>
    </div>
  );
}
