import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { DashboardPage } from "./features/dashboard/DashboardPage";
import { MarketsPage } from "./features/markets/MarketsPage";
import { TradingPage } from "./features/trading/TradingPage";
import { StrategiesPage } from "./features/strategies/StrategiesPage";
import { BacktestPage } from "./features/backtest/BacktestPage";
import { AccountsPage } from "./features/accounts/AccountsPage";
import { PortfolioPage } from "./features/portfolio/PortfolioPage";
import { RuntimePage } from "./features/runtime/RuntimePage";
import { AssistantPage } from "./features/assistant/AssistantPage";

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/markets" element={<MarketsPage />} />
        <Route path="/trading" element={<TradingPage />} />
        <Route path="/runtime" element={<RuntimePage />} />
        <Route path="/strategies" element={<StrategiesPage />} />
        <Route path="/backtest" element={<BacktestPage />} />
        <Route path="/accounts" element={<AccountsPage />} />
        <Route path="/portfolio" element={<PortfolioPage />} />
        <Route path="/assistant" element={<AssistantPage />} />
        <Route path="*" element={<DashboardPage />} />
      </Routes>
    </Layout>
  );
}
