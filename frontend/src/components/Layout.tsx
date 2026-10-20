import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { health } from "../shared/api";
import { useAccount } from "../shared/account-context";
import { useMarketClock } from "../shared/hooks";
import { timeLabel } from "../shared/format";

type NavItem = { to: string; label: string; icon: ReactNode };

function Icon({ path }: { path: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d={path} />
    </svg>
  );
}

const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "工作台",
    items: [
      { to: "/", label: "总览", icon: <Icon path="M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6V11h-6v9Zm0-16v5h6V4h-6Z" /> },
      { to: "/markets", label: "行情", icon: <Icon path="M4 19V5m0 14h16M7 15l3.5-4 3 2.5L20 7" /> },
      { to: "/trading", label: "交易", icon: <Icon path="M4 7h13m0 0-3.5-3.5M17 7l-3.5 3.5M20 17H7m0 0 3.5-3.5M7 17l3.5 3.5" /> },
    ],
  },
  {
    group: "研究",
    items: [
      { to: "/strategies", label: "策略库", icon: <Icon path="M9 3h6v4H9V3Zm-5 7h6v4H4v-4Zm10 0h6v4h-6v-4ZM9 17h6v4H9v-4ZM7 7v3m10 0V7M7 14v3m10 0v-3" /> },
      { to: "/backtest", label: "回测", icon: <Icon path="M4 20V10m5 10V4m5 16v-7m5 7V7" /> },
      { to: "/portfolio", label: "组合", icon: <Icon path="M12 3v18M5 8h14M5 16h14M8 5l-3 3 3 3M16 13l3 3-3 3" /> },
    ],
  },
  {
    group: "系统",
    items: [
      { to: "/accounts", label: "账户", icon: <Icon path="M4 7h16v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7Zm0 0 2-3h12l2 3M8 12h3" /> },
      { to: "/assistant", label: "AI 助手", icon: <Icon path="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Zm6 11 .9 2.1L21 17l-2.1.9L18 20l-.9-2.1L15 17l2.1-.9L18 14Z" /> },
    ],
  },
];

const TITLES: Record<string, { title: string; subtitle: string }> = {
  "/": { title: "总览", subtitle: "账户、持仓与市场的一页视图" },
  "/markets": { title: "行情", subtitle: "多周期 K 线与技术指标" },
  "/trading": { title: "交易", subtitle: "模拟撮合、挂单与持仓管理" },
  "/strategies": { title: "策略库", subtitle: "经典策略模板与参数调试" },
  "/backtest": { title: "回测", subtitle: "事件驱动回测与绩效分析" },
  "/portfolio": { title: "组合", subtitle: "权益曲线、回撤与风险敞口" },
  "/accounts": { title: "账户", subtitle: "资金、账本与账户设置" },
  "/assistant": { title: "AI 助手", subtitle: "用自然语言查询账户与策略" },
};

export function Layout({ children }: { children: ReactNode }) {
  const location = useLocation();
  const meta = TITLES[location.pathname] ?? { title: "LumenQuant", subtitle: "" };
  const { accounts, accountId, setAccountId } = useAccount();
  const clock = useMarketClock();
  const status = useQuery({ queryKey: ["health"], queryFn: health, refetchInterval: 20_000, retry: 1 });
  const online = status.data?.status === "ok";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">LQ</div>
          <div>
            <div className="brand-name">LumenQuant</div>
            <div className="brand-sub">自动化交易工作台</div>
          </div>
        </div>
        <nav>
          {NAV.map((section) => (
            <div key={section.group}>
              <div className="nav-group-label">{section.group}</div>
              {section.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === "/"}
                  className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}
                >
                  {item.icon}
                  <span>{item.label}</span>
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="status-line">
            <span className={`status-dot ${online ? "" : "off"}`} />
            <span>{online ? "服务运行中" : "服务不可用"}</span>
          </div>
          <div className="helper" style={{ marginTop: 6 }}>
            v{status.data?.version ?? "0.1.0"} · 模拟时钟{" "}
            {clock.data ? timeLabel(clock.data.simulated_now) : "—"}
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div>
            <div className="page-title">{meta.title}</div>
            <div className="page-subtitle">{meta.subtitle}</div>
          </div>
          <div className="topbar-spacer" />
          {accounts.length > 0 ? (
            <label className="row" style={{ gap: 8 }}>
              <span className="helper">交易账户</span>
              <select
                className="select"
                style={{ width: 200, height: 34 }}
                value={accountId ?? ""}
                onChange={(event) => setAccountId(event.target.value)}
              >
                {accounts.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} · {item.kind === "paper" ? "模拟" : "实盘"}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <span className={`badge ${online ? "green" : "red"}`}>
            <span className={`status-dot ${online ? "" : "off"}`} style={{ width: 6, height: 6 }} />
            {online ? "已连接" : "离线"}
          </span>
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}
