import { useEffect, useState } from "react";
import { Outlet } from "react-router-dom";

import { usePollEnvelope } from "../../hooks/usePollEnvelope";
import type { Envelope, HealthData, OverviewData, SourcesStatusData } from "../../lib/api";
import { applyTheme, resolveInitialTheme, type ThemeMode } from "../../lib/theme";
import { Banner } from "../primitives/Banner";
import { ContextDrawer } from "./ContextDrawer";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";


export interface ShellOutletContext {
  healthEnvelope: Envelope<HealthData> | null;
  overviewEnvelope: Envelope<OverviewData> | null;
  sourcesEnvelope: Envelope<SourcesStatusData> | null;
  timeRange: string;
}


const CHROME_COMPACT_STORAGE_KEY = "memebot3.ui.chromeCompact";


function resolveInitialChromeCompact() {
  if (typeof window === "undefined") {
    return true;
  }
  const raw = window.localStorage.getItem(CHROME_COMPACT_STORAGE_KEY);
  if (raw === null) {
    return true;
  }
  return raw !== "false";
}


export function AppShell() {
  const [theme, setTheme] = useState<ThemeMode>(() => resolveInitialTheme());
  const [timeRange, setTimeRange] = useState("1h");
  const [isChromeCompact, setIsChromeCompact] = useState(() => resolveInitialChromeCompact());

  const healthQuery = usePollEnvelope<HealthData>("/api/v1/health", 15000);
  const overviewQuery = usePollEnvelope<OverviewData>("/api/v1/overview", 5000);
  const sourcesQuery = usePollEnvelope<SourcesStatusData>("/api/v1/sources/status", 15000);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(CHROME_COMPACT_STORAGE_KEY, String(isChromeCompact));
    }
  }, [isChromeCompact]);

  const orchestrationStatus = overviewQuery.envelope?.data.bot.orchestration_status;
  const shellError = healthQuery.error || overviewQuery.error || sourcesQuery.error;
  const globalBanner = shellError ? (
    <Banner
      detail={shellError}
      title="API request failed"
      tone="danger"
    />
  ) : orchestrationStatus === "stopped" ? (
    <Banner
      detail="The shell is up but the bot is intentionally stopped. Start it from Control Center or from a dedicated console if you want an external run."
      title="Bot stopped"
      tone="info"
    />
  ) : overviewQuery.envelope?.meta.degraded ? (
    <Banner
      detail="The shell is live, but at least one critical source is degraded. The topbar and route surfaces already expose the affected source truth."
      title="Runtime degraded"
      tone="warn"
    />
  ) : overviewQuery.envelope?.meta.stale ? (
    <Banner
      detail="A live snapshot is present but stale. The shell keeps rendering, and each page will surface the exact stale source."
      title="Runtime stale"
      tone="warn"
    />
  ) : null;

  return (
    <div className="app-shell">
      <Sidebar />
      <div className="shell-main">
        <Topbar
          healthEnvelope={healthQuery.envelope}
          isCompact={isChromeCompact}
          onTimeRangeChange={setTimeRange}
          onToggleTheme={() => setTheme((current) => (current === "dark" ? "light" : "dark"))}
          onToggleChrome={() => setIsChromeCompact((current) => !current)}
          overviewEnvelope={overviewQuery.envelope}
          sources={sourcesQuery.envelope?.data.sources || []}
          theme={theme}
          timeRange={timeRange}
        />

        <div className="shell-scroll">
          {globalBanner ? <div className="shell-banner">{globalBanner}</div> : null}
          <main className="shell-canvas">
            <Outlet
              context={{
                healthEnvelope: healthQuery.envelope,
                overviewEnvelope: overviewQuery.envelope,
                sourcesEnvelope: sourcesQuery.envelope,
                timeRange,
              } satisfies ShellOutletContext}
            />
          </main>
        </div>
      </div>
      <ContextDrawer />
    </div>
  );
}
