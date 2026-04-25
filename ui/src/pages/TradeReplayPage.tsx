import { useNavigate, useParams } from "react-router-dom";

import { useDrawer } from "../app/drawer";
import { Banner } from "../components/primitives/Banner";
import { PageHero } from "../components/primitives/PageHero";
import { SourceHealthStrip } from "../components/primitives/SourceHealthStrip";
import { StatusChip } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { TimelineRail } from "../components/primitives/TimelineRail";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type {
  RuntimeEventItem,
  SourceStatus,
  TradeDetailData,
  TradeReplayData,
} from "../lib/api";
import {
  formatCount,
  formatDecimal,
  formatSignedPct,
  formatTimestamp,
  formatUsd,
  humanizeKey,
} from "../lib/format";


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


function shortenToken(value: string | null | undefined) {
  if (!value) {
    return "n/a";
  }
  if (value.length <= 18) {
    return value;
  }
  return `${value.slice(0, 8)}...${value.slice(-6)}`;
}


function statusTone(value: string | null | undefined) {
  if (value === "win") {
    return "success";
  }
  if (value === "fail_timeout") {
    return "warn";
  }
  if (value === "fail") {
    return "danger";
  }
  return "neutral";
}


function mergeSourceStatus(...groups: SourceStatus[][]) {
  return Array.from(
    new Map(groups.flat().map((status) => [status.source_key, status])).values(),
  );
}


const snapshotPriorityKeys = [
  "timestamp",
  "snapshot_at",
  "source_file",
  "discovered_via",
  "entry_regime",
  "price_source",
  "age_minutes",
  "queue_attempts",
  "queue_age_minutes",
  "coverage_core_fields",
  "liquidity_usd",
  "volume_24h_usd",
  "market_cap_usd",
  "txns_last_5m",
  "holders",
];


export function TradeReplayPage() {
  const navigate = useNavigate();
  const { openPanel } = useDrawer();
  const { tradeId } = useParams();
  const parsedTradeId = tradeId && /^\d+$/.test(tradeId) ? Number(tradeId) : null;
  const safeTradeId = parsedTradeId ?? 0;

  const detailQuery = usePollEnvelope<TradeDetailData>(`/api/v1/trades/${safeTradeId}`, 5000);
  const replayQuery = usePollEnvelope<TradeReplayData>(`/api/v1/trades/${safeTradeId}/replay`, 5000);

  const detail = detailQuery.envelope?.data;
  const replay = replayQuery.envelope?.data;
  const trade = detail?.trade || replay?.trade;
  const token = detail?.token || replay?.token;
  const computed = detail?.computed;
  const execution = detail?.execution;
  const runtimeTimeline = replay?.runtime_timeline || [];
  const researchTimeline = replay?.research_timeline || [];
  const sourceStatus = mergeSourceStatus(
    detailQuery.envelope?.meta.source_status || [],
    replayQuery.envelope?.meta.source_status || [],
  );
  const queryError = parsedTradeId === null ? "Trade id is invalid." : detailQuery.error || replayQuery.error;

  function openRecordDrawer(eyebrow: string, title: string, description: string, record: Record<string, unknown>) {
    openPanel({
      eyebrow,
      title,
      description,
      content: (
        <div className="drawer-stack">
          {Object.entries(record).map(([key, value]) => (
            <div className="drawer-kv" key={`${title}-${key}`}>
              <strong>{humanizeKey(key)}</strong>
              <span>{stringifyValue(value)}</span>
            </div>
          ))}
        </div>
      ),
    });
  }

  function openEventDrawer(stream: string, item: RuntimeEventItem) {
    openRecordDrawer(
      `Replay / ${stream}`,
      item.summary,
      `${item.event_type} | ${formatTimestamp(item.ts_utc)}`,
      {
        address: item.address,
        ...item.payload,
      },
    );
  }

  if (!parsedTradeId) {
    return (
      <Surface eyebrow="Inspect / replay" title="Trade replay" subtitle="Invalid trade id">
        <p>The route parameter must be a numeric `trade_id`.</p>
      </Surface>
    );
  }

  if (!detail && !replay && !queryError) {
    return (
      <Surface eyebrow="Inspect / replay" title="Trade replay" subtitle={`Waiting for trade #${parsedTradeId}`}>
        <p>The page is polling the trade detail and replay endpoints for this `trade_id`.</p>
      </Surface>
    );
  }

  if (!trade) {
    return (
      <Surface eyebrow="Inspect / replay" title={`Trade #${parsedTradeId}`} subtitle="Replay unavailable">
        <p>{queryError || "No replay data is currently available for this trade."}</p>
      </Surface>
    );
  }

  const entrySnapshot = replay?.entry_snapshot || null;
  const snapshotEntries = snapshotPriorityKeys
    .filter((key) => entrySnapshot && key in entrySnapshot)
    .map((key) => [key, entrySnapshot?.[key]] as const);
  const derived = replay?.derived;

  return (
    <div className="page-stack">
      <PageHero
        actions={
          <>
            <button className="ui-button ui-button--ghost" onClick={() => navigate("/trades")} type="button">
              Back to trades
            </button>
            {entrySnapshot ? (
              <button
                className="ui-button ui-button--ghost"
                onClick={() =>
                  openRecordDrawer(
                    "Replay / T0 snapshot",
                    `${trade.symbol || trade.address || "Trade"} snapshot`,
                    "Nearest feature snapshot around entry.",
                    entrySnapshot,
                  )
                }
                type="button"
              >
                Open full snapshot
              </button>
            ) : null}
          </>
        }
        eyebrow="Inspect / reconstruction"
        meta={
          <>
            <StatusChip label={`trade #${trade.trade_id}`} tone="info" compact mono />
            <StatusChip label={trade.outcome || "unknown"} tone={statusTone(trade.outcome)} compact mono />
            {trade.exit_reason ? <StatusChip label={trade.exit_reason} tone="neutral" compact mono /> : null}
            {trade.entry_regime ? <StatusChip label={trade.entry_regime} tone="neutral" compact mono /> : null}
          </>
        }
        question="What happened exactly between first sight and final exit for this trade?"
        summary="Replay now reconstructs the trade from DB facts, token context, runtime and research timelines, plus the nearest feature snapshot around entry."
        title={`${trade.symbol || "Unknown"} | replay`}
      />

      {detailQuery.envelope?.meta.degraded || replayQuery.envelope?.meta.degraded ? (
        <Banner
          detail="At least one replay source is degraded. The reconstruction is still rendered, but use the provenance strip before treating it as complete."
          title="Replay degraded"
          tone="warn"
        />
      ) : null}

      {!entrySnapshot ? (
        <Banner
          detail="No entry snapshot was found in parquet for this address. Runtime and research timelines are still available."
          title="T0 snapshot missing"
          tone="info"
        />
      ) : null}

      {queryError ? (
        <Banner
          detail={queryError}
          title="Replay query failed"
          tone="danger"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface className="grid-span-8" eyebrow="Trade factsheet" title={`${trade.symbol || trade.address || "Trade"} facts`}>
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Address</span>
              <strong>{shortenToken(trade.address)}</strong>
            </div>
            <div className="kv-cell">
              <span>Opened</span>
              <strong>{formatTimestamp(trade.opened_at)}</strong>
            </div>
            <div className="kv-cell">
              <span>Closed</span>
              <strong>{trade.closed_at ? formatTimestamp(trade.closed_at) : "still open"}</strong>
            </div>
            <div className="kv-cell">
              <span>Buy amount</span>
              <strong>{formatDecimal(trade.buy_amount_sol, " SOL")}</strong>
            </div>
            <div className="kv-cell">
              <span>Total PnL</span>
              <strong>{formatSignedPct(trade.total_pnl_pct)}</strong>
            </div>
            <div className="kv-cell">
              <span>Total PnL USD</span>
              <strong>{formatUsd(trade.total_pnl_usd)}</strong>
            </div>
            <div className="kv-cell">
              <span>Entry AI</span>
              <strong>{formatDecimal(trade.entry_ai_proba)}</strong>
            </div>
            <div className="kv-cell">
              <span>Peak PnL</span>
              <strong>{formatSignedPct(trade.highest_pnl_pct)}</strong>
            </div>
            <div className="kv-cell">
              <span>Buy price</span>
              <strong>{formatUsd(trade.buy_price_usd)}</strong>
            </div>
            <div className="kv-cell">
              <span>Exit price</span>
              <strong>{formatUsd(trade.effective_exit_price_usd || trade.close_price_usd)}</strong>
            </div>
            <div className="kv-cell">
              <span>Size bucket</span>
              <strong>{trade.size_bucket || "n/a"}</strong>
            </div>
            <div className="kv-cell">
              <span>Hold minutes</span>
              <strong>{formatDecimal(derived?.hold_minutes ?? computed?.hold_minutes, "m")}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Token context" title={token?.symbol || "Token metadata"}>
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Name</span>
              <strong>{token?.name || "n/a"}</strong>
            </div>
            <div className="kv-cell">
              <span>Discovered via</span>
              <strong>{token?.discovered_via || "n/a"}</strong>
            </div>
            <div className="kv-cell">
              <span>Liquidity</span>
              <strong>{formatUsd(token?.liquidity_usd)}</strong>
            </div>
            <div className="kv-cell">
              <span>Market cap</span>
              <strong>{formatUsd(token?.market_cap_usd)}</strong>
            </div>
            <div className="kv-cell">
              <span>Holders</span>
              <strong>{formatCount(token?.holders)}</strong>
            </div>
            <div className="kv-cell">
              <span>Score total</span>
              <strong>{formatDecimal(token?.score_total)}</strong>
            </div>
            <div className="kv-cell">
              <span>Trend</span>
              <strong>{token?.trend || "n/a"}</strong>
            </div>
            <div className="kv-cell">
              <span>Dex</span>
              <strong>{token?.dex_id || "n/a"}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-8" eyebrow="Pnl reconstruction" title="Computed totals">
          <div className="metric-ribbon">
            <div className="metric-ribbon__item">
              <span>Total cost</span>
              <strong>{formatUsd(computed?.total_cost_usd)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Total proceeds</span>
              <strong>{formatUsd(computed?.total_proceeds_usd)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Realized PnL</span>
              <strong>{formatUsd(computed?.realized_pnl_usd)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Unrealized PnL</span>
              <strong>{formatUsd(computed?.unrealized_pnl_usd)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Realized qty</span>
              <strong>{formatDecimal(computed?.realized_qty)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Remaining qty</span>
              <strong>{formatDecimal(computed?.remaining_qty)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Execution" title="Buy, close and partials">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Buy tx</span>
              <strong>{shortenToken(execution?.buy_tx_sig)}</strong>
            </div>
            <div className="kv-cell">
              <span>Exit tx</span>
              <strong>{shortenToken(execution?.exit_tx_sig)}</strong>
            </div>
            <div className="kv-cell">
              <span>Price source at buy</span>
              <strong>{execution?.price_source_at_buy || "n/a"}</strong>
            </div>
            <div className="kv-cell">
              <span>Price source at close</span>
              <strong>{execution?.price_source_at_close || "n/a"}</strong>
            </div>
            <div className="kv-cell">
              <span>Partial taken</span>
              <strong>{execution?.partial_taken ? "yes" : "no"}</strong>
            </div>
            <div className="kv-cell">
              <span>Partial count</span>
              <strong>{formatCount(execution?.partial_count)}</strong>
            </div>
            <div className="kv-cell">
              <span>First partial</span>
              <strong>{execution?.first_partial_at ? formatTimestamp(execution.first_partial_at) : "n/a"}</strong>
            </div>
            <div className="kv-cell">
              <span>Last partial qty</span>
              <strong>{formatDecimal(execution?.last_partial_qty)}</strong>
            </div>
          </div>
        </Surface>

        <Surface
          className="grid-span-8"
          eyebrow="T0 snapshot"
          title="Nearest feature row around entry"
          subtitle={entrySnapshot ? "The replay uses the nearest parquet snapshot available for this address." : "No parquet snapshot was available."}
        >
          {snapshotEntries.length ? (
            <div className="kv-grid">
              {snapshotEntries.map(([key, value]) => (
                <div className="kv-cell" key={key}>
                  <span>{humanizeKey(key)}</span>
                  <strong>{stringifyValue(value)}</strong>
                </div>
              ))}
            </div>
          ) : (
            <p className="empty-note">No entry snapshot fields are available for this trade.</p>
          )}
        </Surface>

        <Surface className="grid-span-4" eyebrow="Derived timings" title="From first sight to exit">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>First seen at</span>
              <strong>{formatTimestamp(derived?.first_seen_at)}</strong>
            </div>
            <div className="kv-cell">
              <span>First seen to buy</span>
              <strong>{formatDecimal(derived?.minutes_first_seen_to_buy, "m")}</strong>
            </div>
            <div className="kv-cell">
              <span>Hold duration</span>
              <strong>{formatDecimal(derived?.hold_minutes ?? computed?.hold_minutes, "m")}</strong>
            </div>
            <div className="kv-cell">
              <span>Runtime events</span>
              <strong>{formatCount(runtimeTimeline.length)}</strong>
            </div>
            <div className="kv-cell">
              <span>Research events</span>
              <strong>{formatCount(researchTimeline.length)}</strong>
            </div>
            <div className="kv-cell">
              <span>Outcome</span>
              <strong>{trade.outcome || computed?.outcome || "n/a"}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-6" eyebrow="Runtime rail" title="Operational timeline">
          <TimelineRail
            emptyMessage="No runtime events available for this trade."
            items={runtimeTimeline}
            onSelect={(item) => openEventDrawer("runtime", item)}
          />
        </Surface>

        <Surface className="grid-span-6" eyebrow="Research rail" title="Research timeline">
          <TimelineRail
            emptyMessage="No research events available for this trade."
            items={researchTimeline}
            onSelect={(item) => openEventDrawer("research", item)}
          />
        </Surface>

        <Surface className="grid-span-12" eyebrow="Source truth" title="Replay provenance">
          <SourceHealthStrip sources={sourceStatus} />
        </Surface>
      </div>
    </div>
  );
}
