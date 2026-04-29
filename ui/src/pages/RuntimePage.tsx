import { useOutletContext } from "react-router-dom";

import { useDrawer } from "../app/drawer";
import type { ShellOutletContext } from "../components/layout/AppShell";
import { Banner } from "../components/primitives/Banner";
import { ChartShell } from "../components/primitives/ChartShell";
import { PageHero } from "../components/primitives/PageHero";
import { RuntimePulse } from "../components/primitives/RuntimePulse";
import { StatusChip } from "../components/primitives/StatusChip";
import { StrategyHealthStrip } from "../components/primitives/StrategyHealthStrip";
import { Surface } from "../components/primitives/Surface";
import { TimelineRail } from "../components/primitives/TimelineRail";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type {
  RuntimeEventItem,
  RuntimeEventsData,
  RuntimeStateData,
  RuntimeStrategyHealthData,
  StrategyHealthEntry,
} from "../lib/api";
import { formatCount, formatDecimal, formatTimestamp, humanizeKey } from "../lib/format";


function stringifyValue(value: unknown) {
  if (value === null || value === undefined) {
    return "n/a";
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}


function numericRecordRows(record: Record<string, number> | undefined) {
  return Object.entries(record || {})
    .map(([group, count]) => ({ group, count }))
    .sort((left, right) => right.count - left.count || left.group.localeCompare(right.group));
}


export function RuntimePage() {
  const { timeRange } = useOutletContext<ShellOutletContext>();
  const { openPanel } = useDrawer();

  const runtimeStateQuery = usePollEnvelope<RuntimeStateData>("/api/v1/runtime/state", 3000);
  const strategyHealthQuery = usePollEnvelope<RuntimeStrategyHealthData>("/api/v1/runtime/strategy-health", 3000);
  const runtimeEventsQuery = usePollEnvelope<RuntimeEventsData>("/api/v1/runtime/events?limit=14", 2500);

  const runtimeState = runtimeStateQuery.envelope?.data;
  const strategyHealth = strategyHealthQuery.envelope?.data.strategy_health || runtimeState?.strategy_health || {};
  const runtimeEvents = runtimeEventsQuery.envelope?.data.items || [];
  const healthEnvelope = strategyHealthQuery.envelope?.data;
  const queryError = runtimeStateQuery.error || strategyHealthQuery.error || runtimeEventsQuery.error;

  if (!runtimeState) {
    return (
      <Surface eyebrow="Monitor / runtime" title="Runtime" subtitle="Waiting for the first runtime snapshot">
        <p>The page is polling `/api/v1/runtime/state`, `/api/v1/runtime/strategy-health`, and `/api/v1/runtime/events`.</p>
      </Surface>
    );
  }
  const state = runtimeState;

  function openBuildDrawer() {
    openPanel({
      eyebrow: "Runtime / build info",
      title: state.build_info.app || "Runtime build",
      description: "Build and process metadata from the persisted runtime snapshot.",
      content: (
        <div className="drawer-stack">
          {Object.entries(state.build_info).map(([key, value]) => (
            <div className="drawer-kv" key={key}>
              <strong>{humanizeKey(key)}</strong>
              <span>{stringifyValue(value)}</span>
            </div>
          ))}
        </div>
      ),
    });
  }

  function openStrategyDrawer(regime: string, item: StrategyHealthEntry) {
    openPanel({
      eyebrow: "Runtime / strategy health",
      title: regime.replaceAll("_", " "),
      description: "Live strategy-health payload for this regime.",
      content: (
        <div className="drawer-stack">
          {Object.entries(item).map(([key, value]) => (
            <div className="drawer-kv" key={`${regime}-${key}`}>
              <strong>{humanizeKey(key)}</strong>
              <span>{stringifyValue(value)}</span>
            </div>
          ))}
        </div>
      ),
    });
  }

  function openEventDrawer(item: RuntimeEventItem) {
    openPanel({
      eyebrow: "Runtime / event",
      title: item.summary,
      description: `${item.event_type} · ${formatTimestamp(item.ts_utc)}`,
      content: (
        <div className="drawer-stack">
          <div className="drawer-kv">
            <strong>Address</strong>
            <span>{item.address || "global event"}</span>
          </div>
          {Object.entries(item.payload).map(([key, value]) => (
            <div className="drawer-kv" key={`${item.id}-${key}`}>
              <strong>{humanizeKey(key)}</strong>
              <span>{stringifyValue(value)}</span>
            </div>
          ))}
        </div>
      ),
    });
  }

  const metaRow = (
    <>
      <StatusChip label={runtimeState.process_state || "unknown"} tone={runtimeStateQuery.envelope?.meta.degraded ? "danger" : "success"} />
      <StatusChip label={runtimeState.discovery_paused ? "discovery paused" : "discovery live"} tone={runtimeState.discovery_paused ? "warn" : "success"} />
      <StatusChip label={runtimeState.buys_paused ? "buys paused" : "buys live"} tone={runtimeState.buys_paused ? "warn" : "success"} />
      <StatusChip label={runtimeState.dry_run ? "dry run" : "live capital"} tone={runtimeState.dry_run ? "info" : "success"} />
    </>
  );

  return (
    <div className="page-stack">
      <PageHero
        actions={
          <button className="ui-button ui-button--ghost" onClick={openBuildDrawer} type="button">
            Open build info
          </button>
        }
        eyebrow="Monitor / live runtime console"
        meta={metaRow}
        question="Which part of the runtime is failing, paused, rate-limited, or backing off right now?"
        summary="Runtime is now the live operator console: heartbeat, pause flags, buy limiter, strategy health, and event rail are all visible from the persisted bot snapshot and the append-only runtime feed."
        title="Runtime console"
      />

      {runtimeStateQuery.envelope?.meta.degraded || runtimeStateQuery.envelope?.meta.stale ? (
        <Banner
          detail="The persisted runtime snapshot is not fresh. This page keeps rendering the last known state and pairs it with the append-only event stream so the operator still has context."
          title="Runtime state not fresh"
          tone="warn"
        />
      ) : null}

      {state.last_error ? (
        <Banner
          detail={`${state.last_error}${state.last_error_at ? ` · ${formatTimestamp(state.last_error_at)}` : ""}`}
          title="Last runtime error"
          tone="danger"
        />
      ) : null}

      {queryError ? (
        <Banner
          detail={queryError}
          title="Runtime query failed"
          tone="danger"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface className="grid-span-8" eyebrow="Heartbeat and process" title={state.process_state || "Unknown process state"} subtitle={`Snapshot updated ${formatTimestamp(state.updated_at)}`}>
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Heartbeat</span>
              <strong>{formatTimestamp(state.heartbeat_at)}</strong>
            </div>
            <div className="kv-cell">
              <span>Started</span>
              <strong>{formatTimestamp(state.started_at)}</strong>
            </div>
            <div className="kv-cell">
              <span>Host</span>
              <strong>{state.build_info.hostname || "n/a"}</strong>
            </div>
            <div className="kv-cell">
              <span>PID</span>
              <strong>{formatDecimal(state.build_info.pid)}</strong>
            </div>
            <div className="kv-cell">
              <span>Python</span>
              <strong>{state.build_info.python_version || "n/a"}</strong>
            </div>
            <div className="kv-cell">
              <span>Bot version</span>
              <strong>{state.build_info.bot_version || "local"}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Queue and limiter" title="Pressure and exposure">
          <div className="funnel-grid">
            <div className="funnel-step">
              <span>Open positions</span>
              <strong>{formatCount(state.open_positions_count)}</strong>
            </div>
            <div className="funnel-step">
              <span>Queue pending</span>
              <strong>{formatCount(state.queue_pending)}</strong>
            </div>
            <div className="funnel-step">
              <span>Queue requeued</span>
              <strong>{formatCount(state.queue_requeued)}</strong>
            </div>
            <div className="funnel-step">
              <span>Queue cooldown</span>
              <strong>{formatCount(state.queue_cooldown)}</strong>
            </div>
            <div className="funnel-step">
              <span>Buys in window</span>
              <strong>{formatCount(state.buy_limiter_in_window)}</strong>
            </div>
            <div className="funnel-step">
              <span>Limiter window</span>
              <strong>{formatDecimal(state.buy_limiter_window_s, "s")}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-12" eyebrow="Strategy health" title="Regime posture" subtitle="Current health snapshot per regime">
          <StrategyHealthStrip items={strategyHealth} onSelect={openStrategyDrawer} />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Productive health" title="PnL validation lane">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Productive trades</span>
              <strong>{formatCount(healthEnvelope?.productive_trade_count)}</strong>
            </div>
            <div className="kv-cell">
              <span>Productive avg PnL</span>
              <strong>{formatDecimal(healthEnvelope?.productive_avg_pnl_pct, "%")}</strong>
            </div>
            <div className="kv-cell">
              <span>Productive win rate</span>
              <strong>{formatDecimal(healthEnvelope?.productive_win_rate, "%")}</strong>
            </div>
            <div className="kv-cell">
              <span>Severe exits rolling</span>
              <strong>{formatCount(healthEnvelope?.severe_exits_rolling)}</strong>
            </div>
            <div className="kv-cell">
              <span>Cooldown until</span>
              <strong>{formatTimestamp(healthEnvelope?.cooldown_until)}</strong>
            </div>
            <div className="kv-cell">
              <span>Last disable reason</span>
              <strong>{healthEnvelope?.last_disable_reason || "n/a"}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Lane counts" title="Entry lanes observed">
          <div className="breakdown-list">
            {numericRecordRows(healthEnvelope?.entry_lane_counts).slice(0, 8).map((row) => (
              <div className="breakdown-list__item" key={row.group}>
                <div className="breakdown-list__label">
                  <strong>{row.group}</strong>
                  <span>{formatCount(row.count)}</span>
                </div>
              </div>
            ))}
            {!numericRecordRows(healthEnvelope?.entry_lane_counts).length ? <p className="empty-note">No lane counts in health snapshot.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Sniper rejects" title="Top reject reasons">
          <div className="breakdown-list">
            {numericRecordRows(healthEnvelope?.sniper_reject_reasons).slice(0, 8).map((row) => (
              <div className="breakdown-list__item" key={row.group}>
                <div className="breakdown-list__label">
                  <strong>{row.group}</strong>
                  <span>{formatCount(row.count)}</span>
                </div>
              </div>
            ))}
            {!numericRecordRows(healthEnvelope?.sniper_reject_reasons).length ? <p className="empty-note">No sniper reject reasons in health snapshot.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Workers and guards" title="Background processes">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Discovery last ok</span>
              <strong>{formatTimestamp(state.discovery_last_ok_at)}</strong>
            </div>
            <div className="kv-cell">
              <span>Monitor last ok</span>
              <strong>{formatTimestamp(state.monitor_last_ok_at)}</strong>
            </div>
            <div className="kv-cell">
              <span>Retrain state</span>
              <strong>{state.retrain_state || "n/a"}</strong>
            </div>
            <div className="kv-cell">
              <span>Reports refresh</span>
              <strong>{state.reports_refresh_state || "n/a"}</strong>
            </div>
            <div className="kv-cell">
              <span>ML gate</span>
              <strong>{state.ml_gate.mode || "n/a"}</strong>
            </div>
            <div className="kv-cell">
              <span>Wallet SOL</span>
              <strong>{formatDecimal(state.wallet_sol, " SOL")}</strong>
            </div>
          </div>
        </Surface>

        <ChartShell
          caption={runtimeEvents[0] ? `Latest event ${formatTimestamp(runtimeEvents[0].ts_utc)} · range preset ${timeRange}` : "No recent runtime events"}
          className="grid-span-8"
          subtitle="Event density and mix from the append-only runtime feed."
          title="Runtime pulse"
        >
          <RuntimePulse items={runtimeEvents} />
        </ChartShell>

        <Surface className="grid-span-12" eyebrow="Runtime timeline" title="Latest events" subtitle="Append-only operational trace">
          <TimelineRail emptyMessage="No runtime events available." items={runtimeEvents} onSelect={openEventDrawer} />
        </Surface>
      </div>
    </div>
  );
}
