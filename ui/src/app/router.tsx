import { createBrowserRouter, Navigate } from "react-router-dom";

import { ProtectedLayout } from "./ProtectedLayout";
import { AnalyticsPage } from "../pages/AnalyticsPage";
import { ConfigCenterPage } from "../pages/ConfigCenterPage";
import { ControlCenterPage } from "../pages/ControlCenterPage";
import { DiscoveryPage } from "../pages/DiscoveryPage";
import { LoginPage } from "../pages/LoginPage";
import { LogsPage } from "../pages/LogsPage";
import { MlCenterPage } from "../pages/MlCenterPage";
import { OverviewPage } from "../pages/OverviewPage";
import { PolicyCenterPage } from "../pages/PolicyCenterPage";
import { PositionsPage } from "../pages/PositionsPage";
import { QueuePage } from "../pages/QueuePage";
import { RuntimePage } from "../pages/RuntimePage";
import { SniperPage } from "../pages/SniperPage";
import { TradeReplayPage } from "../pages/TradeReplayPage";
import { TradesPage } from "../pages/TradesPage";


export const router = createBrowserRouter([
  {
    path: "/login",
    element: <LoginPage />,
  },
  {
    path: "/",
    element: <ProtectedLayout />,
    children: [
      {
        index: true,
        element: <Navigate to="/overview" replace />,
      },
      {
        path: "overview",
        element: <OverviewPage />,
      },
      {
        path: "runtime",
        element: <RuntimePage />,
      },
      {
        path: "sniper",
        element: <SniperPage />,
      },
      {
        path: "discovery",
        element: <DiscoveryPage />,
      },
      {
        path: "queue",
        element: <QueuePage />,
      },
      {
        path: "positions",
        element: <PositionsPage />,
      },
      {
        path: "trades",
        element: <TradesPage />,
      },
      {
        path: "trades/:tradeId",
        element: <TradeReplayPage />,
      },
      {
        path: "analytics",
        element: <AnalyticsPage />,
      },
      {
        path: "ml",
        element: <MlCenterPage />,
      },
      {
        path: "policy",
        element: <PolicyCenterPage />,
      },
      {
        path: "config",
        element: <ConfigCenterPage />,
      },
      {
        path: "logs",
        element: <LogsPage />,
      },
      {
        path: "control",
        element: <ControlCenterPage />,
      },
    ],
  },
]);
