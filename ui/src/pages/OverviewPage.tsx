import { useNavigate, useOutletContext } from "react-router-dom";

import { useDrawer } from "../app/drawer";
import type { ShellOutletContext } from "../components/layout/AppShell";
import { Banner } from "../components/primitives/Banner";
import { ChartShell } from "../components/primitives/ChartShell";
import { DataTable, type DataColumn } from "../components/primitives/DataTable";
import { PageHero } from "../components/primitives/PageHero";
import { RuntimePulse } from "../components/primitives/RuntimePulse";
import { StatusChip, toneFromStatus } from "../components/primitives/StatusChip";
import { StrategyHealthStrip } from "../components/primitives/StrategyHealthStrip";
import { Surface } from "../components/primitives/Surface";
import { TimelineRail } from "../components/primitives/TimelineRail";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type {
  RuntimeEventItem,
  RuntimeEventsData,
  RuntimeStateData,
  RuntimeStrategyHealthData,
  SourceStatus,
  StrategyHealthEntry,
} from "../lib/api";
import { formatCount, formatDecimal, formatPct, formatRelative, formatTimestamp, humanizeKey, shortenPath } from "../lib/format";


function sourceColumns(): DataColumn<SourceStatus>[] {
  return [
    {
      id: "source",
      header: "Source",
      mono: true,
      render: (row) => humanizeKey(row.source_key),
    },
    {
      id: "status",
      header: "Status",
      render: (row) => <StatusChip compact label={row.status} tone={toneFromStatus(row.status)} mono />,
    },
    {
      id: "updated",
      header: "Updated",
      render: (row) => (row.updated_at ? `${formatRelative(row.updated_at)} ago` : "n/a"),
    },
    {
      id: "detail",
      header: "Detail",
      render: (row) => row.detail || shortenPath(row.path),
    },
  ];
}


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


export function OverviewPage() {
  const navigate = useNavigate();
  const { overviewEnvelope, sourcesEnvelope } = useOutletContext<ShellOutletContext>();
  const { openPanel } = useDrawer();
  const overview = overviewEnvelope?.data;
  const sources = sourcesEnvelope?.data.sources || [];

  const runtimeStateQuery = usePollEnvelope<RuntimeStateData>("/api/v1/runtime/state", 3000);
  const strategyHealthQuery = usePollEnvelope<RuntimeStrategyHealthData>("/api/v1/runtime/strategy-health", 3000);
  const runtimeEventsQuery = usePollEnvelope<RuntimeEventsData>("/api/v1/runtime/events?limit=10", 5000);

  const runtimeState = runtimeStateQuery.envelope?.data;
  const strategyHealth = strategyHealthQuery.envelope?.data.strategy_health || runtimeState?.strategy_health || {};
  const runtimeEvents = runtimeEventsQuery.envelope?.data.items || [];
  const runtimeQueryError = runtimeStateQuery.error || strategyHealthQuery.error || runtimeEventsQuery.error;

  if (!overview) {
    return (
      <Surface eyebrow="Monitor / overview" title="Overview" subtitle="Waiting for the first overview payload">
        <p>The shell is mounted and polling `/api/v1/overview`.</p>
      </Surface>
    );
  }

  const orchestrationLabel = overview.bot.orchestration_status || overview.bot.staleness || "unknown";
  const orchestrationTone = toneFromStatus(
    overview.bot.orchestration_status === "running_managed" ? "running" : (overview.bot.orchestration_status || overview.bot.staleness || "off") as never,
  );

  const metaRow = (
    <>
      <StatusChip label={orchestrationLabel} tone={orchestrationTone} />
      <StatusChip
        label={
          overview.runtime.discovery_paused === null
            ? "discovery offline"
            : overview.runtime.discovery_paused
              ? "discovery paused"
              : "discovery live"
        }
        tone={overview.runtime.discovery_paused === null ? "neutral" : overview.runtime.discovery_paused ? "warn" : "success"}
      />
      <StatusChip
        label={
          overview.runtime.buys_paused === null
            ? "buys offline"
            : overview.runtime.buys_paused
              ? "buys paused"
              : "buys live"
        }
        tone={overview.runtime.buys_paused === null ? "neutral" : overview.runtime.buys_paused ? "warn" : "success"}
      />
      <StatusChip
        label={overview.bot.dry_run === null ? "bot offline" : overview.bot.dry_run ? "dry run" : "live capital"}
        tone={overview.bot.dry_run === null ? "neutral" : overview.bot.dry_run ? "info" : "success"}
      />
    </>
  );

  const routeTiles = [
    {
      path: "/runtime",
      label: "Runtime",
      value: overview.bot.process_state || overview.bot.orchestration_status || "unknown",
      meta:
        overview.bot.orchestration_status === "stopped"
          ? "bot currently stopped"
          : overview.bot.heartbeat_at
            ? `heartbeat ${formatRelative(overview.bot.heartbeat_at)} ago`
            : "no heartbeat",
    },
    {
      path: "/queue",
      label: "Queue",
      value: formatCount(overview.queue.pending),
      meta: `${formatCount(overview.queue.requeued)} requeued · ${formatCount(overview.queue.cooldown)} cooldown`,
    },
    {
      path: "/positions",
      label: "Positions",
      value: formatCount(overview.positions.open_rows),
      meta: `${formatCount(overview.positions.closed_rows)} closed · ${formatPct(overview.positions.win_rate_pct)} win`,
    },
    {
      path: "/ml",
      label: "ML",
      value: overview.ml.model_loaded ? "loaded" : "missing",
      meta: `threshold ${formatDecimal(overview.ml.threshold)}`,
    },
    {
      path: "/config",
      label: "Wallet / policy",
      value: formatDecimal(overview.wallet.wallet_sol, " SOL"),
      meta: overview.wallet.wallet_checked_at ? `checked ${formatTimestamp(overview.wallet.wallet_checked_at)}` : "wallet unchecked",
    },
    {
      path: "/logs",
      label: "Research",
      value: formatCount(overview.research.open_shadow_count),
      meta: overview.research.scorecard_generated_at ? `scorecard ${formatTimestamp(overview.research.scorecard_generated_at)}` : "no scorecard",
    },
  ];

  function openStrategyDrawer(regime: string, item: StrategyHealthEntry) {
    openPanel({
      eyebrow: "Overview / strategy health",
      title: regime.replaceAll("_", " "),
      description: "Current strategy health snapshot for this regime.",
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
      eyebrow: "Overview / runtime event",
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

  return (
    <div className="page-stack">
      <PageHero
        actions={
          <button className="ui-button ui-button--ghost" onClick={() => navigate("/runtime")} type="button">
            Open runtime console
          </button>
        }
        eyebrow="Monitor / first daily console"
        meta={metaRow}
        question="Is the bot alive, coherent, and worth watching right now, or should I intervene?"
        summary="Overview is now a true daily cockpit: queue, wallet, funnel, live pulse, and strategy posture are visible without leaving the UI."
        title="Operational overview"
      />

      {overviewEnvelope?.meta.degraded || runtimeStateQuery.envelope?.meta.degraded ? (
        <Banner
          detail="At least one runtime source is degraded. Overview stays readable, but the shell now points you straight into Runtime with the same evidence."
          title="Overview degraded"
          tone="warn"
        />
      ) : null}

      {runtimeQueryError ? (
        <Banner
          detail={runtimeQueryError}
          title="Runtime preview request failed"
          tone="danger"
        />
      ) : null}

      {!overview.ml.model_loaded ? (
        <Banner
          detail="The daily cockpit remains usable, but ML is currently not loaded. Threshold and gate state stay visible so the operator sees the exact posture."
          title="ML unavailable"
          tone="info"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface className="grid-span-8" eyebrow="Queue, wallet, and positions" title={overview.bot.process_state || "Unknown runtime state"} subtitle={`Heartbeat ${formatTimestamp(overview.bot.heartbeat_at)}`}>
          <div className="route-tiles">
            {routeTiles.map((tile) => (
              <button className="route-tile" key={tile.path} onClick={() => navigate(tile.path)} type="button">
                <span>{tile.label}</span>
                <strong>{tile.value}</strong>
                <small>{tile.meta}</small>
              </button>
            ))}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Funnel snapshot" title="Runtime funnel">
          <div className="funnel-grid">
            <div className="funnel-step">
              <span>Raw discovered</span>
              <strong>{formatCount(runtimeState?.stats.raw_discovered)}</strong>
            </div>
            <div className="funnel-step">
              <span>Filtered out</span>
              <strong>{formatCount(runtimeState?.stats.filtered_out)}</strong>
            </div>
            <div className="funnel-step">
              <span>AI pass</span>
              <strong>{formatCount(runtimeState?.stats.ai_pass)}</strong>
            </div>
            <div className="funnel-step">
              <span>Bought</span>
              <strong>{formatCount(runtimeState?.stats.bought)}</strong>
            </div>
            <div className="funnel-step">
              <span>Sold</span>
              <strong>{formatCount(runtimeState?.stats.sold)}</strong>
            </div>
            <div className="funnel-step">
              <span>Requeues</span>
              <strong>{formatCount(runtimeState?.stats.requeues)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-12" eyebrow="Strategy health" title="Regime posture" subtitle="Current health snapshot from the runtime state and strategy-health endpoint">
          <StrategyHealthStrip items={strategyHealth} onSelect={openStrategyDrawer} />
        </Surface>

        <ChartShell
          caption={runtimeEvents[0] ? `Latest event ${formatTimestamp(runtimeEvents[0].ts_utc)}` : "No runtime event preview yet"}
          className="grid-span-8"
          subtitle="Recent event mix gives a compact pulse of what the bot is actually doing now."
          title="Runtime pulse"
        >
          <RuntimePulse items={runtimeEvents} />
        </ChartShell>

        <Surface className="grid-span-4" eyebrow="Latest runtime events" title="Timeline preview" subtitle="Fast path into Runtime">
          <TimelineRail emptyMessage="No recent runtime events." items={runtimeEvents.slice(0, 5)} onSelect={openEventDrawer} />
        </Surface>

        <Surface className="grid-span-12" eyebrow="Source health" title="Truth strip expanded" subtitle="Exact provenance for overview and the pages it drills into">
          <DataTable columns={sourceColumns()} rowKey={(row) => row.source_key} rows={sources} />
        </Surface>
      </div>
    </div>
  );
}
