import { useDeferredValue, useState } from "react";
import { useOutletContext } from "react-router-dom";

import { useDrawer } from "../app/drawer";
import type { ShellOutletContext } from "../components/layout/AppShell";
import { Banner } from "../components/primitives/Banner";
import { ChartShell } from "../components/primitives/ChartShell";
import { PageHero } from "../components/primitives/PageHero";
import { SavedViewsToolbar } from "../components/primitives/SavedViewsToolbar";
import { SourceHealthStrip } from "../components/primitives/SourceHealthStrip";
import { StatusChip } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type {
  DiscoveryCounterRow,
  DiscoveryFeedData,
  DiscoveryFeedItem,
  DiscoverySummaryData,
  RequeueReasonRow,
} from "../lib/api";
import { formatCompact, formatCount, formatRelative, formatTimestamp, humanizeKey } from "../lib/format";


const stageOptions = ["all", "queue", "strategy", "ml", "execution", "new", "scored"];
const actionOptions = ["all", "rejected", "wait", "shadow", "bought", "requeue", "blocked", "passed"];


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


function timeRangeToWindowMinutes(value: string) {
  switch (value) {
    case "15m":
      return 15;
    case "24h":
      return 24 * 60;
    case "1h":
    default:
      return 60;
  }
}


function buildPath(pathname: string, params: Record<string, string | number | null | undefined>) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === null || value === undefined || value === "") {
      return;
    }
    search.set(key, String(value));
  });
  const query = search.toString();
  return query ? `${pathname}?${query}` : pathname;
}


function severityTone(item: DiscoveryFeedItem) {
  switch (item.severity) {
    case "success":
      return "success";
    case "warning":
      return "warn";
    case "danger":
      return "danger";
    default:
      return "info";
  }
}


function streamTone(stream: DiscoveryFeedItem["stream"]) {
  return stream === "runtime" ? "info" : "neutral";
}


function maxBucketCount<Row>(rows: Row[], getValue: (row: Row) => number) {
  return rows.reduce((current, row) => Math.max(current, getValue(row)), 0);
}


function reasonOptionsFromSummary(summary: DiscoverySummaryData | undefined) {
  const groups = new Set<string>();
  (summary?.candidate_decisions || []).forEach((row) => {
    const segments = row.group.split(":");
    const reason = segments.slice(1).join(":");
    if (reason) {
      groups.add(reason);
    }
  });
  (summary?.requeue_reasons || []).forEach((row) => {
    if (row.reason) {
      groups.add(row.reason);
    }
  });
  return Array.from(groups).slice(0, 8);
}


export function DiscoveryPage() {
  const { timeRange } = useOutletContext<ShellOutletContext>();
  const { openPanel } = useDrawer();
  const [stageFilter, setStageFilter] = useState("all");
  const [actionFilter, setActionFilter] = useState("all");
  const [reasonFilter, setReasonFilter] = useState("");
  const [addressFilter, setAddressFilter] = useState("");
  const deferredAddress = useDeferredValue(addressFilter.trim());
  const windowMin = timeRangeToWindowMinutes(timeRange);

  const summaryQuery = usePollEnvelope<DiscoverySummaryData>(
    buildPath("/api/v1/discovery/summary", { window_min: windowMin }),
    5000,
  );
  const feedQuery = usePollEnvelope<DiscoveryFeedData>(
    buildPath("/api/v1/discovery/feed", {
      limit: 24,
      address: deferredAddress || undefined,
      stage: stageFilter === "all" ? undefined : stageFilter,
      decision_action: actionFilter === "all" ? undefined : actionFilter,
      reason: reasonFilter || undefined,
    }),
    5000,
  );

  const summary = summaryQuery.envelope?.data;
  const feed = feedQuery.envelope?.data;
  const sourceStatus = feedQuery.envelope?.meta.source_status || summaryQuery.envelope?.meta.source_status || [];
  const queryError = summaryQuery.error || feedQuery.error;
  const reasonOptions = reasonOptionsFromSummary(summary);
  const topDecisionCount = maxBucketCount(summary?.candidate_decisions || [], (row) => row.count);
  const topStageCount = maxBucketCount(summary?.candidate_stages || [], (row) => row.count);
  const topRequeueCount = maxBucketCount(summary?.requeue_reasons || [], (row) => row.events);

  function clearFilters() {
    setStageFilter("all");
    setActionFilter("all");
    setReasonFilter("");
    setAddressFilter("");
  }

  function applySavedView(filters: Record<string, unknown>) {
    setStageFilter(typeof filters.stageFilter === "string" ? filters.stageFilter : "all");
    setActionFilter(typeof filters.actionFilter === "string" ? filters.actionFilter : "all");
    setReasonFilter(typeof filters.reasonFilter === "string" ? filters.reasonFilter : "");
    setAddressFilter(typeof filters.addressFilter === "string" ? filters.addressFilter : "");
  }

  function openFeedDrawer(item: DiscoveryFeedItem) {
    openPanel({
      eyebrow: "Discovery / feed item",
      title: item.summary,
      description: `${item.stream} | ${item.event_type} | ${formatTimestamp(item.ts_utc)}`,
      content: (
        <div className="drawer-stack">
          <div className="drawer-kv">
            <strong>Address</strong>
            <span>{item.address || "global event"}</span>
          </div>
          <div className="drawer-kv">
            <strong>Symbol</strong>
            <span>{item.symbol || "n/a"}</span>
          </div>
          <div className="drawer-kv">
            <strong>Regime</strong>
            <span>{item.regime || "n/a"}</span>
          </div>
          <div className="drawer-kv">
            <strong>Stage</strong>
            <span>{item.stage || "n/a"}</span>
          </div>
          <div className="drawer-kv">
            <strong>Action</strong>
            <span>{item.action || "n/a"}</span>
          </div>
          <div className="drawer-kv">
            <strong>Reason</strong>
            <span>{item.reason || "n/a"}</span>
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

  if (!summary && !feed && !queryError) {
    return (
      <Surface eyebrow="Monitor / discovery" title="Discovery funnel" subtitle="Waiting for the first discovery payloads">
        <p>The page is polling `/api/v1/discovery/feed` and `/api/v1/discovery/summary`.</p>
      </Surface>
    );
  }

  return (
    <div className="page-stack">
      <PageHero
        eyebrow="Monitor / discovery funnel"
        meta={
          <>
            <StatusChip label={`${windowMin}m window`} tone="info" compact />
            <StatusChip
              label={feedQuery.envelope?.meta.degraded ? "feed degraded" : "feed live"}
              tone={feedQuery.envelope?.meta.degraded ? "warn" : "success"}
            />
            <StatusChip label={`${formatCount(feed?.count)} items`} tone="neutral" compact />
            {deferredAddress ? <StatusChip label={`address ${deferredAddress}`} tone="info" compact mono /> : null}
          </>
        }
        question="Where is the discovery funnel collapsing before a buy happens?"
        summary="Discovery now exposes the live reject, wait, shadow, requeue, and buy path as a single operator surface with filters that match the API contract."
        title="Discovery funnel"
      />

      {feedQuery.envelope?.meta.degraded || summaryQuery.envelope?.meta.degraded ? (
        <Banner
          detail="At least one append-only feed is degraded. Discovery still renders the last readable funnel and keeps the exact source truth visible."
          title="Discovery degraded"
          tone="warn"
        />
      ) : null}

      {feedQuery.envelope?.meta.stale || summaryQuery.envelope?.meta.stale ? (
        <Banner
          detail="The discovery feeds are stale. Counts and reasons remain visible, but the newest decisions may not be represented yet."
          title="Discovery stale"
          tone="warn"
        />
      ) : null}

      {queryError ? (
        <Banner
          detail={queryError}
          title="Discovery query failed"
          tone="danger"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface
          className="grid-span-12"
          eyebrow="Filters"
          title="Feed controls"
          subtitle="Stage, action, reason, and address filters are mapped directly to the backend query contract."
          actions={
            <button className="ui-button ui-button--ghost" onClick={clearFilters} type="button">
              Clear filters
            </button>
          }
        >
          <div className="filter-stack">
            <div className="filter-row">
              <label className="filter-field">
                <span>Address</span>
                <input
                  className="ui-field"
                  onChange={(event) => setAddressFilter(event.target.value)}
                  placeholder="token address"
                  type="search"
                  value={addressFilter}
                />
              </label>
              <label className="filter-field">
                <span>Reason</span>
                <input
                  className="ui-field"
                  onChange={(event) => setReasonFilter(event.target.value)}
                  placeholder="no_liq, confirm_snapshots, buy_ok"
                  type="search"
                  value={reasonFilter}
                />
              </label>
            </div>

            <div className="filter-field">
              <span>Stage</span>
              <div className="choice-row">
                {stageOptions.map((option) => (
                  <button
                    className={["choice-chip", stageFilter === option ? "choice-chip--active" : ""].filter(Boolean).join(" ")}
                    key={option}
                    onClick={() => setStageFilter(option)}
                    type="button"
                  >
                    {option}
                  </button>
                ))}
              </div>
            </div>

            <div className="filter-field">
              <span>Action</span>
              <div className="choice-row">
                {actionOptions.map((option) => (
                  <button
                    className={["choice-chip", actionFilter === option ? "choice-chip--active" : ""].filter(Boolean).join(" ")}
                    key={option}
                    onClick={() => setActionFilter(option)}
                    type="button"
                  >
                    {option}
                  </button>
                ))}
              </div>
            </div>

            {reasonOptions.length ? (
              <div className="filter-field">
                <span>Quick reason picks</span>
                <div className="choice-row">
                  {reasonOptions.map((option) => (
                    <button
                      className={["choice-chip", reasonFilter === option ? "choice-chip--active" : ""].filter(Boolean).join(" ")}
                      key={option}
                      onClick={() => setReasonFilter(option)}
                      type="button"
                    >
                      {option}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}

            <SavedViewsToolbar
              currentFilters={{ stageFilter, actionFilter, reasonFilter, addressFilter }}
              onApply={applySavedView}
              pageKey="discovery"
            />
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Queue pressure" title="Runtime queue actions" subtitle={`Window preset ${timeRange}`}>
          <div className="funnel-grid">
            <div className="funnel-step">
              <span>Added</span>
              <strong>{formatCount(summary?.queue.added)}</strong>
            </div>
            <div className="funnel-step">
              <span>Requeued</span>
              <strong>{formatCount(summary?.queue.requeued)}</strong>
            </div>
            <div className="funnel-step">
              <span>Dropped</span>
              <strong>{formatCount(summary?.queue.dropped)}</strong>
            </div>
            <div className="funnel-step">
              <span>Bought</span>
              <strong>{formatCount(summary?.queue.bought)}</strong>
            </div>
          </div>
        </Surface>

        <ChartShell
          caption={summary?.candidate_decisions?.length ? `${summary.candidate_decisions.length} decision buckets in current window` : "No candidate decisions in the selected window"}
          className="grid-span-8"
          subtitle="Research decisions are ranked by count so the operator sees where the funnel is spending its time."
          title="Candidate decision breakdown"
        >
          <div className="breakdown-list">
            {(summary?.candidate_decisions || []).slice(0, 8).map((row) => (
              <div className="breakdown-list__item" key={row.group}>
                <div className="breakdown-list__label">
                  <strong>{row.group}</strong>
                  <span>{formatCompact(row.count)}</span>
                </div>
                <div className="breakdown-list__bar">
                  <span style={{ width: `${topDecisionCount ? (row.count / topDecisionCount) * 100 : 0}%` }} />
                </div>
              </div>
            ))}
            {!summary?.candidate_decisions?.length ? <p className="empty-note">No candidate decisions recorded in the selected window.</p> : null}
          </div>
        </ChartShell>

        <Surface className="grid-span-4" eyebrow="Research stage mix" title="Stage breakdown">
          <div className="breakdown-list">
            {(summary?.candidate_stages || []).slice(0, 8).map((row) => (
              <div className="breakdown-list__item" key={row.group}>
                <div className="breakdown-list__label">
                  <strong>{row.group}</strong>
                  <span>{formatCount(row.count)}</span>
                </div>
                <div className="breakdown-list__bar">
                  <span style={{ width: `${topStageCount ? (row.count / topStageCount) * 100 : 0}%` }} />
                </div>
              </div>
            ))}
            {!summary?.candidate_stages?.length ? <p className="empty-note">No stage events available.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Retry pressure" title="Requeue reasons">
          <div className="breakdown-list">
            {(summary?.requeue_reasons || []).slice(0, 8).map((row) => (
              <div className="breakdown-list__item" key={row.reason}>
                <div className="breakdown-list__label">
                  <strong>{row.reason}</strong>
                  <span>{formatCount(row.events)}</span>
                </div>
                <div className="breakdown-list__bar breakdown-list__bar--warn">
                  <span style={{ width: `${topRequeueCount ? (row.events / topRequeueCount) * 100 : 0}%` }} />
                </div>
              </div>
            ))}
            {!summary?.requeue_reasons?.length ? <p className="empty-note">No requeue reasons in the current window.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Source truth" title="Discovery provenance">
          <SourceHealthStrip sources={sourceStatus} />
        </Surface>

        <Surface
          className="grid-span-12"
          eyebrow="Unified feed"
          title="Latest decisions"
          subtitle={feed?.filters.before_ts ? `Showing items before ${feed.filters.before_ts}` : "Most recent runtime and research decisions"}
        >
          <div className="decision-feed">
            {(feed?.items || []).map((item) => (
              <button className="decision-card" key={item.id} onClick={() => openFeedDrawer(item)} type="button">
                <div className="decision-card__header">
                  <div className="decision-card__meta">
                    <StatusChip compact label={item.stream} mono tone={streamTone(item.stream)} />
                    {item.stage ? <StatusChip compact label={item.stage} mono tone="neutral" /> : null}
                    {item.action ? <StatusChip compact label={item.action} mono tone={severityTone(item)} /> : null}
                    {item.reason ? <StatusChip compact label={item.reason} mono tone="neutral" /> : null}
                  </div>
                  <time dateTime={item.ts_utc} title={formatTimestamp(item.ts_utc)}>
                    {formatRelative(item.ts_utc)} ago
                  </time>
                </div>

                <div className="decision-card__summary">
                  <strong>{item.summary}</strong>
                  <p>{item.symbol || item.address || "global discovery signal"}</p>
                </div>

                <div className="decision-card__footer">
                  <span>{item.address || "global event"}</span>
                  <small>{item.regime ? `regime ${item.regime}` : item.event_type}</small>
                </div>
              </button>
            ))}
            {!feed?.items?.length ? <p className="empty-note">No discovery items match the current filters.</p> : null}
          </div>
        </Surface>
      </div>
    </div>
  );
}
