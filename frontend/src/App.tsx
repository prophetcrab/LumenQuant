import { Suspense, lazy } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Loader } from "./components/ui";

// Route-level code splitting keeps the initial bundle to the shell + dashboard.
const DashboardPage = lazy(() =>
  import("./features/dashboard/DashboardPage").then((m) => ({ default: m.DashboardPage })),
);
const MarketsPage = lazy(() =>
  import("./features/markets/MarketsPage").then((m) => ({ default: m.MarketsPage })),
);
const TradingPage = lazy(() =>
  import("./features/trading/TradingPage").then((m) => ({ default: m.TradingPage })),
);
const RuntimePage = lazy(() =>
  import("./features/runtime/RuntimePage").then((m) => ({ default: m.RuntimePage })),
);
const StrategiesPage = lazy(() =>
  import("./features/strategies/StrategiesPage").then((m) => ({ default: m.StrategiesPage })),
);
const BacktestPage = lazy(() =>
  import("./features/backtest/BacktestPage").then((m) => ({ default: m.BacktestPage })),
);
const PortfolioPage = lazy(() =>
  import("./features/portfolio/PortfolioPage").then((m) => ({ default: m.PortfolioPage })),
);
const AccountsPage = lazy(() =>
  import("./features/accounts/AccountsPage").then((m) => ({ default: m.AccountsPage })),
);
const AssistantPage = lazy(() =>
  import("./features/assistant/AssistantPage").then((m) => ({ default: m.AssistantPage })),
);

export default function App() {
  return (
    <Layout>
      <Suspense fallback={<Loader label="加载页面" />}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/markets" element={<MarketsPage />} />
          <Route path="/trading" element={<TradingPage />} />
          <Route path="/runtime" element={<RuntimePage />} />
          <Route path="/strategies" element={<StrategiesPage />} />
          <Route path="/backtest" element={<BacktestPage />} />
          <Route path="/portfolio" element={<PortfolioPage />} />
          <Route path="/accounts" element={<AccountsPage />} />
          <Route path="/assistant" element={<AssistantPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </Layout>
  );
}
