import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../shared/api";
import type { AssistantReply } from "../../shared/api";
import { useAccount } from "../../shared/account-context";
import { Badge, Card, Loader } from "../../components/ui";
import { ButtonField } from "../../components/form";

type Message =
  | { role: "user"; text: string }
  | { role: "assistant"; reply: AssistantReply }
  | { role: "error"; text: string };

const SUGGESTIONS = [
  "我的账户情况怎么样？",
  "看看我的持仓",
  "BTC 现在什么价",
  "RSI 在 ETH 1h 的信号",
  "回测双均线最近 800 根",
  "有哪些策略",
  "自动运行实例状态",
];

export function AssistantPage() {
  const { accountId } = useAccount();
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Message[]>([
    {
      role: "assistant",
      reply: {
        answer:
          "你好，我是 LumenQuant 的本地研究助手。我可以读取账户、持仓、订单、行情、策略与回测结果；" +
          "所有工具都是只读的，不会替你下单。试试下面的问题，或直接把你的问题写下来。",
        mode: "local",
        tool_calls: [],
      },
    },
  ]);
  const endRef = useRef<HTMLDivElement | null>(null);
  const status = useQuery({ queryKey: ["assistant-status"], queryFn: api.assistantStatus });

  const chat = useMutation({
    mutationFn: (message: string) => api.assistantChat(message, accountId),
    onSuccess: (reply) => {
      setMessages((prev) => [...prev, { role: "assistant", reply }]);
    },
    onError: (error: Error) => {
      setMessages((prev) => [...prev, { role: "error", text: error.message }]);
    },
  });

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, chat.isPending]);

  const send = (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || chat.isPending) return;
    setMessages((prev) => [...prev, { role: "user", text: trimmed }]);
    setInput("");
    chat.mutate(trimmed);
  };

  return (
    <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr) 320px", alignItems: "start" }}>
      <Card
        title="对话"
        subtitle="只读工具 · 不执行真实交易"
        actions={
          <Badge tone={status.data?.mode === "llm" ? "indigo" : ""}>
            {status.data?.mode === "llm" ? `LLM · ${status.data.model}` : "本地模式"}
          </Badge>
        }
        padded={false}
      >
        <div
          style={{
            height: "calc(100vh - 290px)",
            minHeight: 460,
            overflowY: "auto",
            padding: "18px 20px",
            display: "flex",
            flexDirection: "column",
            gap: 14,
          }}
        >
          {messages.map((message, index) => {
            if (message.role === "user") {
              return (
                <div key={index} style={{ alignSelf: "flex-end", maxWidth: "74%" }}>
                  <div
                    style={{
                      background: "var(--accent)",
                      color: "#fff",
                      padding: "10px 14px",
                      borderRadius: "14px 14px 4px 14px",
                      fontSize: 13.5,
                      lineHeight: 1.6,
                      whiteSpace: "pre-wrap",
                    }}
                  >
                    {message.text}
                  </div>
                </div>
              );
            }
            if (message.role === "error") {
              return (
                <div key={index} style={{ alignSelf: "flex-start", maxWidth: "78%" }}>
                  <div
                    style={{
                      background: "var(--negative-soft)",
                      color: "var(--negative)",
                      padding: "10px 14px",
                      borderRadius: "14px 14px 14px 4px",
                      fontSize: 13,
                    }}
                  >
                    {message.text}
                  </div>
                </div>
              );
            }
            return (
              <div key={index} style={{ alignSelf: "flex-start", maxWidth: "82%" }}>
                <div
                  style={{
                    background: "var(--surface-subtle)",
                    border: "1px solid var(--border)",
                    padding: "12px 15px",
                    borderRadius: "14px 14px 14px 4px",
                    fontSize: 13.5,
                    lineHeight: 1.7,
                    whiteSpace: "pre-wrap",
                  }}
                >
                  {message.reply.answer}
                </div>
                {message.reply.tool_calls.length > 0 ? (
                  <div className="row" style={{ gap: 6, marginTop: 6, flexWrap: "wrap" }}>
                    {message.reply.tool_calls.map((call, callIndex) => (
                      <span key={callIndex} className="badge indigo" style={{ fontSize: 10.5 }}>
                        工具 · {call.tool}
                      </span>
                    ))}
                    <span className="helper">{message.reply.mode === "llm" ? "模型回答" : "本地推理"}</span>
                  </div>
                ) : null}
              </div>
            );
          })}
          {chat.isPending ? (
            <div className="row" style={{ alignSelf: "flex-start", gap: 8, color: "var(--text-muted)", fontSize: 12.5 }}>
              <span className="spinner" /> 正在调用工具…
            </div>
          ) : null}
          <div ref={endRef} />
        </div>
        <div style={{ borderTop: "1px solid var(--border)", padding: 14 }}>
          <div className="row" style={{ alignItems: "flex-end", gap: 10 }}>
            <textarea
              className="textarea"
              style={{ minHeight: 46, maxHeight: 140, flex: 1 }}
              placeholder="例如：我的持仓风险如何？回测 MACD 在 BTC 1h 最近 800 根。"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  send(input);
                }
              }}
            />
            <ButtonField disabled={chat.isPending || !input.trim()} onClick={() => send(input)} style={{ height: 46 }}>
              发送
            </ButtonField>
          </div>
          <div className="helper" style={{ marginTop: 8 }}>
            Enter 发送，Shift+Enter 换行。助手仅做事实查询与解释，不构成投资建议。
          </div>
        </div>
      </Card>

      <div className="stack" style={{ gap: 14 }}>
        <Card title="快捷问题" subtitle="点击直接提问">
          <div className="stack" style={{ gap: 7 }}>
            {SUGGESTIONS.map((item) => (
              <button
                key={item}
                className="button ghost"
                style={{ justifyContent: "flex-start", height: "auto", padding: "8px 10px", fontSize: 12.5, textAlign: "left" }}
                onClick={() => send(item)}
                disabled={chat.isPending}
              >
                {item}
              </button>
            ))}
          </div>
        </Card>
        <Card title="可用工具" subtitle="全部只读">
          <ToolsList />
        </Card>
      </div>
    </div>
  );
}

function ToolsList() {
  const tools = useQuery({ queryKey: ["assistant-tools"], queryFn: api.assistantTools, staleTime: 60_000 });
  if (tools.isLoading) return <Loader label="读取工具" />;
  return (
    <div className="stack" style={{ gap: 9 }}>
      {(tools.data ?? []).map((tool) => (
        <div key={tool.name}>
          <div className="mono" style={{ fontSize: 11.5, color: "var(--accent)", fontWeight: 600 }}>
            {tool.name}
          </div>
          <div className="helper" style={{ marginTop: 1, lineHeight: 1.55 }}>
            {tool.description}
          </div>
        </div>
      ))}
    </div>
  );
}
