import { useDeferredValue, useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { useDrawer } from "../../app/drawer";
import { useAuth } from "../../auth/AuthProvider";
import type { Envelope, HealthData, OverviewData, SourceStatus } from "../../lib/api";
import { formatRelative } from "../../lib/format";
import type { ThemeMode } from "../../lib/theme";
import { SourceHealthStrip } from "../primitives/SourceHealthStrip";
import { StatusChip, toneFromStatus } from "../primitives/StatusChip";


interface TopbarProps {
  healthEnvelope: Envelope<HealthData> | null;
  overviewEnvelope: Envelope<OverviewData> | null;
  sources: SourceStatus[];
  theme: ThemeMode;
  onToggleTheme: () => void;
  isCompact: boolean;
  onToggleChrome: () => void;
  timeRange: string;
  onTimeRangeChange: (nextRange: string) => void;
}

const timeRanges = ["15m", "1h", "24h"];


function resolveRuntimeLabel(overviewEnvelope: Envelope<OverviewData> | null) {
  const overview = overviewEnvelope?.data;
  if (!overviewEnvelope || !overview) {
    return { label: "loading", tone: "info" as const };
  }
  if (overview.bot.orchestration_status === "stopped") {
    return { label: "bot stopped", tone: "neutral" as const };
  }
  if (overview.bot.orchestration_status === "starting") {
    return { label: "bot starting", tone: "info" as const };
  }
  if (overview.bot.orchestration_status === "running_external") {
    return { label: "bot external", tone: "info" as const };
  }
  if (overview.bot.orchestration_status === "crashed") {
    return { label: "bot crashed", tone: "danger" as const };
  }
  if (overviewEnvelope.meta.degraded || overview.bot.staleness === "error") {
    return { label: "runtime degraded", tone: "danger" as const };
  }
  if (overviewEnvelope.meta.stale || overview.bot.staleness === "stale") {
    return { label: "runtime stale", tone: "warn" as const };
  }
  if (overview.runtime.buys_paused || overview.runtime.discovery_paused) {
    return { label: "runtime paused", tone: "warn" as const };
  }
  return { label: overview.bot.process_state || "runtime ready", tone: "success" as const };
}


export function Topbar({
  healthEnvelope,
  overviewEnvelope,
  sources,
  theme,
  onToggleTheme,
  isCompact,
  onToggleChrome,
  timeRange,
  onTimeRangeChange,
}: TopbarProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const { openPanel } = useDrawer();
  const { session, hasPermission, logout } = useAuth();
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query);
  const runtimeTone = resolveRuntimeLabel(overviewEnvelope);
  const pathLabel = location.pathname === "/" ? "/overview" : location.pathname;
  const currentUser = session?.user;
  const canPauseDiscovery = hasPermission("control.command.pause_discovery");
  const canPauseBuys = hasPermission("control.command.pause_buys");
  const searchHint = deferredQuery.trim()
    ? /^\d+$/.test(deferredQuery.trim())
      ? `Open trade replay #${deferredQuery.trim()}`
      : deferredQuery.trim().startsWith("/")
        ? `Jump to ${deferredQuery.trim()}`
        : "Open trades context and keep this query in drawer"
    : "Search trade_id, address, symbol, or /route";

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = query.trim();
    if (!trimmed) {
      return;
    }

    if (/^\d+$/.test(trimmed)) {
      navigate(`/trades/${trimmed}`);
      return;
    }

    if (trimmed.startsWith("/")) {
      navigate(trimmed);
      return;
    }

    openPanel({
      eyebrow: "Context search",
      title: `Search seed: ${trimmed}`,
      description: "Global omnibox is wired in the shell. Domain pages will turn this into real filters in PR-UI-9 to PR-UI-12.",
      content: (
        <div className="drawer-stack">
          <p>
            The shell already captures the operator query and routes it into context. For now it acts as a jump-off point
            to the trades workspace.
          </p>
          <p className="drawer-note">Suggested next hop: `/trades` for `trade_id`, address, or symbol filtering.</p>
        </div>
      ),
    });
    navigate("/trades");
  }

  function openQuickAction(command: "pause_discovery" | "pause_buys") {
    navigate(`/control?command=${command}`);
  }

  return (
    <header className={["topbar", isCompact ? "topbar--compact" : ""].filter(Boolean).join(" ")}>
      <div className="topbar__statusline">
        <div className="topbar__brandline">
          <StatusChip label={runtimeTone.label} tone={runtimeTone.tone} />
          <StatusChip
            label={healthEnvelope?.data.status || "api"}
            tone={toneFromStatus((healthEnvelope?.meta.source_status[0]?.status || "ok") as never)}
            compact
          />
          {currentUser ? <StatusChip label={currentUser.role} tone="neutral" compact mono /> : null}
          {currentUser ? <StatusChip label={currentUser.username} tone="info" compact mono /> : null}
          <small>{pathLabel}</small>
        </div>
        <div className="topbar__meta">
          <small>overview {formatRelative(overviewEnvelope?.meta.generated_at)} ago</small>
          <small>theme {theme}</small>
          <button className="ui-button ui-button--ghost topbar__collapse-button" onClick={onToggleChrome} type="button">
            {isCompact ? "Expand shell" : "Collapse shell"}
          </button>
        </div>
      </div>

      {isCompact ? null : (
        <div className="topbar__controls">
          <SourceHealthStrip sources={sources} />

          <form className="topbar__search" onSubmit={submitSearch}>
            <input
              aria-label="Search trade id, token address, symbol, or route"
              onChange={(event) => setQuery(event.target.value)}
              placeholder="trade_id, address, symbol, /route"
              type="search"
              value={query}
            />
            <button className="ui-button ui-button--primary" type="submit">
              Search
            </button>
            <small>{searchHint}</small>
          </form>

          <div className="topbar__actions">
            <div className="segmented-control" aria-label="Time range">
              {timeRanges.map((option) => (
                <button
                  className={["segmented-control__item", timeRange === option ? "is-active" : ""].filter(Boolean).join(" ")}
                  key={option}
                  onClick={() => onTimeRangeChange(option)}
                  type="button"
                >
                  {option}
                </button>
              ))}
            </div>

            <button
              className="ui-button ui-button--ghost"
              disabled={!canPauseDiscovery}
              onClick={() => openQuickAction("pause_discovery")}
              type="button"
            >
              {canPauseDiscovery ? "Pause discovery" : "Discovery locked"}
            </button>
            <button
              className="ui-button ui-button--ghost"
              disabled={!canPauseBuys}
              onClick={() => openQuickAction("pause_buys")}
              type="button"
            >
              Pause buys
            </button>
            <button className="ui-button ui-button--ghost" onClick={onToggleTheme} type="button">
              {theme === "dark" ? "Light mode" : "Dark mode"}
            </button>
            {session?.auth_mode === "local" ? (
              <button className="ui-button ui-button--ghost" onClick={() => void logout()} type="button">
                Sign out
              </button>
            ) : null}
          </div>
        </div>
      )}
    </header>
  );
}
