import { useDeferredValue, useState } from "react";

import { useDrawer } from "../app/drawer";
import { Banner } from "../components/primitives/Banner";
import { DataTable, type DataColumn } from "../components/primitives/DataTable";
import { PageHero } from "../components/primitives/PageHero";
import { SavedViewsToolbar } from "../components/primitives/SavedViewsToolbar";
import { SourceHealthStrip } from "../components/primitives/SourceHealthStrip";
import { StatusChip } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type { QueueItem, QueueItemsData, QueueSummaryData } from "../lib/api";
import { formatCount, formatDecimal, formatRelative, formatTimestamp, humanizeKey } from "../lib/format";


const queueStatusOptions = ["all", "pending", "requeued", "cooldown"];


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


function queueStatusTone(status: string | null | undefined) {
  if (status === "pending") {
    return "info";
  }
  if (status === "requeued" || status === "cooldown") {
    return "warn";
  }
  return "neutral";
}


export function QueuePage() {
  const { openPanel } = useDrawer();
  const [statusFilter, setStatusFilter] = useState("all");
  const [addressFilter, setAddressFilter] = useState("");
  const deferredAddress = useDeferredValue(addressFilter.trim());

  const summaryQuery = usePollEnvelope<QueueSummaryData>("/api/v1/queue/summary", 3000);
  const itemsQuery = usePollEnvelope<QueueItemsData>(
    buildPath("/api/v1/queue/items", {
      limit: 50,
      status: statusFilter === "all" ? undefined : statusFilter,
      address: deferredAddress || undefined,
    }),
    3000,
  );

  const summary = summaryQuery.envelope?.data;
  const items = itemsQuery.envelope?.data.items || [];
  const queryError = summaryQuery.error || itemsQuery.error;
  const sourceStatus = itemsQuery.envelope?.meta.source_status || summaryQuery.envelope?.meta.source_status || [];

  function clearFilters() {
    setStatusFilter("all");
    setAddressFilter("");
  }

  function applySavedView(filters: Record<string, unknown>) {
    setStatusFilter(typeof filters.statusFilter === "string" ? filters.statusFilter : "all");
    setAddressFilter(typeof filters.addressFilter === "string" ? filters.addressFilter : "");
  }

  function openQueueDrawer(item: QueueItem) {
    openPanel({
      eyebrow: "Queue / candidate",
      title: item.symbol || item.address || "Queued candidate",
      description: `${item.status || "unknown"} | ${item.last_reason || "no reason"} | ${formatTimestamp(item.first_seen_at)}`,
      content: (
        <div className="drawer-stack">
          {Object.entries(item).map(([key, value]) => (
            <div className="drawer-kv" key={`${item.address || "queue-item"}-${key}`}>
              <strong>{humanizeKey(key)}</strong>
              <span>{stringifyValue(value)}</span>
            </div>
          ))}
        </div>
      ),
    });
  }

  function queueColumns(): DataColumn<QueueItem>[] {
    return [
      {
        id: "token",
        header: "Token",
        render: (row) => (
          <button className="mono-link-button table-primary-cell" onClick={() => openQueueDrawer(row)} type="button">
            <strong>{row.symbol || "Unknown"}</strong>
            <small>{row.address || "n/a"}</small>
          </button>
        ),
      },
      {
        id: "status",
        header: "Status",
        render: (row) => <StatusChip compact label={row.status || "unknown"} mono tone={queueStatusTone(row.status)} />,
      },
      {
        id: "age",
        align: "right",
        header: "Age",
        render: (row) => formatDecimal(row.queue_age_minutes, "m"),
      },
      {
        id: "attempts",
        align: "right",
        header: "Attempts",
        render: (row) => formatCount(row.attempts),
      },
      {
        id: "retries",
        align: "right",
        header: "Retries left",
        render: (row) => formatCount(row.retries_left),
      },
      {
        id: "next",
        header: "Next retry",
        render: (row) => (row.next_retry_at ? formatTimestamp(row.next_retry_at) : "-"),
      },
      {
        id: "reason",
        header: "Last reason",
        render: (row) => row.last_reason || "n/a",
      },
    ];
  }

  if (!summary && !itemsQuery.envelope && !queryError) {
    return (
      <Surface eyebrow="Monitor / queue" title="Queue backlog" subtitle="Waiting for the first queue payload">
        <p>The page is polling `/api/v1/queue/summary` and `/api/v1/queue/items`.</p>
      </Surface>
    );
  }

  return (
    <div className="page-stack">
      <PageHero
        eyebrow="Monitor / queue backlog"
        meta={
          <>
            <StatusChip
              label={itemsQuery.envelope?.meta.degraded ? "snapshot degraded" : "snapshot live"}
              tone={itemsQuery.envelope?.meta.degraded ? "warn" : "success"}
            />
            <StatusChip label={`${formatCount(summary?.pending)} pending`} tone="info" compact />
            <StatusChip label={`${formatCount(summary?.requeued)} requeued`} tone="warn" compact />
            <StatusChip label={`${formatCount(summary?.cooldown)} cooldown`} tone="neutral" compact />
          </>
        }
        question="What is waiting in the queue, and why is it still there?"
        summary="Queue now exposes the live backlog, retry posture, and oldest candidate pressure from the persisted runtime snapshot without coupling the UI to the bot loop."
        title="Queue backlog"
      />

      {itemsQuery.envelope?.meta.degraded ? (
        <Banner
          detail="The queue snapshot is not fresh or is missing queue item detail. Summary counters may still be available, but the table is only the last known snapshot."
          title="Queue degraded"
          tone="warn"
        />
      ) : null}

      {itemsQuery.envelope?.meta.empty ? (
        <Banner
          detail="No queue items are currently visible for the selected filter set. The page stays valid and keeps showing summary and retry pressure."
          title="Queue empty"
          tone="info"
        />
      ) : null}

      {queryError ? (
        <Banner
          detail={queryError}
          title="Queue query failed"
          tone="danger"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface
          className="grid-span-12"
          eyebrow="Filters"
          title="Queue controls"
          subtitle="Status and address filters are applied against the persisted queue snapshot."
          actions={
            <button className="ui-button ui-button--ghost" onClick={clearFilters} type="button">
              Clear filters
            </button>
          }
        >
          <div className="filter-stack">
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

            <div className="filter-field">
              <span>Status</span>
              <div className="choice-row">
                {queueStatusOptions.map((option) => (
                  <button
                    className={["choice-chip", statusFilter === option ? "choice-chip--active" : ""].filter(Boolean).join(" ")}
                    key={option}
                    onClick={() => setStatusFilter(option)}
                    type="button"
                  >
                    {option}
                  </button>
                ))}
              </div>
            </div>

            <SavedViewsToolbar
              currentFilters={{ statusFilter, addressFilter }}
              onApply={applySavedView}
              pageKey="queue"
            />
          </div>
        </Surface>

        <Surface className="grid-span-8" eyebrow="Snapshot summary" title="Backlog pressure" subtitle={`Captured ${formatTimestamp(summary?.captured_at)}`}>
          <div className="funnel-grid">
            <div className="funnel-step">
              <span>Pending</span>
              <strong>{formatCount(summary?.pending)}</strong>
            </div>
            <div className="funnel-step">
              <span>Requeued</span>
              <strong>{formatCount(summary?.requeued)}</strong>
            </div>
            <div className="funnel-step">
              <span>Cooldown</span>
              <strong>{formatCount(summary?.cooldown)}</strong>
            </div>
            <div className="funnel-step">
              <span>Oldest first seen</span>
              <strong>{summary?.oldest_first_seen_at ? `${formatRelative(summary.oldest_first_seen_at)} ago` : "n/a"}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Source truth" title="Queue provenance">
          <SourceHealthStrip sources={sourceStatus} />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Retry pressure" title="Recent requeue reasons">
          <div className="breakdown-list">
            {(summary?.recent_requeue_reasons || []).slice(0, 8).map((row) => (
              <div className="breakdown-list__item" key={row.reason}>
                <div className="breakdown-list__label">
                  <strong>{row.reason}</strong>
                  <span>{formatCount(row.events)}</span>
                </div>
                <div className="breakdown-list__bar breakdown-list__bar--warn">
                  <span
                    style={{
                      width: `${summary?.recent_requeue_reasons?.[0]?.events ? (row.events / summary.recent_requeue_reasons[0].events) * 100 : 0}%`,
                    }}
                  />
                </div>
              </div>
            ))}
            {!summary?.recent_requeue_reasons?.length ? <p className="empty-note">No recent requeue reasons.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-8" eyebrow="Current queue" title="Live items" subtitle={`${formatCount(itemsQuery.envelope?.data.count)} visible rows`}>
          <DataTable
            columns={queueColumns()}
            emptyMessage="No queue items match the current filters."
            rowKey={(row) => `${row.address || "queue"}-${row.status || "status"}-${row.first_seen_at || "ts"}`}
            rows={items}
          />
        </Surface>
      </div>
    </div>
  );
}
