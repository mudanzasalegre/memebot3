import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Banner } from "../components/primitives/Banner";
import { DataTable, type DataColumn } from "../components/primitives/DataTable";
import { PageHero } from "../components/primitives/PageHero";
import { SavedViewsToolbar } from "../components/primitives/SavedViewsToolbar";
import { SourceHealthStrip } from "../components/primitives/SourceHealthStrip";
import { StatusChip } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type { ClosedTradeItem, ClosedTradesData } from "../lib/api";
import { formatCount, formatDecimal, formatSignedPct, formatTimestamp, formatUsd } from "../lib/format";


const outcomeOptions = ["all", "win", "fail", "fail_timeout"];
const pageSizeOptions = [25, 50, 100, 150, 200];

interface PageCursor {
  before_id: number | null;
  before_ts: string | null;
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


function countBy(items: ClosedTradeItem[], key: (item: ClosedTradeItem) => string) {
  const counts = new Map<string, number>();
  items.forEach((item) => {
    const bucket = key(item);
    counts.set(bucket, (counts.get(bucket) || 0) + 1);
  });
  return Array.from(counts.entries())
    .map(([group, count]) => ({ group, count }))
    .sort((left, right) => right.count - left.count || left.group.localeCompare(right.group));
}


function maxCount(rows: Array<{ count: number }>) {
  return rows.reduce((current, row) => Math.max(current, row.count), 0);
}


function outcomeTone(value: string | null | undefined) {
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


export function TradesPage() {
  const navigate = useNavigate();
  const [outcomeFilter, setOutcomeFilter] = useState("all");
  const [exitReasonFilter, setExitReasonFilter] = useState("");
  const [entryRegimeFilter, setEntryRegimeFilter] = useState("");
  const [entryLaneFilter, setEntryLaneFilter] = useState("");
  const [gateProfileFilter, setGateProfileFilter] = useState("");
  const [dexFilter, setDexFilter] = useState("");
  const [proxyFilter, setProxyFilter] = useState("");
  const [pageLimit, setPageLimit] = useState(50);
  const [cursorHistory, setCursorHistory] = useState<PageCursor[]>([{ before_id: null, before_ts: null }]);
  const [cursorIndex, setCursorIndex] = useState(0);

  const currentCursor = cursorHistory[cursorIndex] || { before_id: null, before_ts: null };

  const tradesQuery = usePollEnvelope<ClosedTradesData>(
    buildPath("/api/v1/trades/closed", {
      limit: pageLimit,
      before_ts: currentCursor.before_ts || undefined,
      before_id: currentCursor.before_id || undefined,
      outcome: outcomeFilter === "all" ? undefined : outcomeFilter,
      exit_reason: exitReasonFilter || undefined,
      entry_regime: entryRegimeFilter || undefined,
      entry_lane: entryLaneFilter || undefined,
      gate_profile: gateProfileFilter || undefined,
      buy_dex_id: dexFilter || undefined,
      liquidity_proxy: proxyFilter || undefined,
    }),
    5000,
  );

  const tradesData = tradesQuery.envelope?.data;
  const trades = tradesData?.items || [];
  const sourceStatus = tradesQuery.envelope?.meta.source_status || [];
  const summary = tradesData?.summary;
  const consistency = tradesData?.consistency;
  const totalCount = tradesData?.total_count ?? trades.length;
  const pageCount = tradesData?.page_count ?? trades.length;
  const hasMore = tradesData?.has_more ?? false;
  const nextBeforeTs = tradesData?.next_before_ts ?? null;
  const nextBeforeId = tradesData?.next_before_id ?? null;

  useEffect(() => {
    setCursorHistory([{ before_id: null, before_ts: null }]);
    setCursorIndex(0);
  }, [dexFilter, entryLaneFilter, entryRegimeFilter, exitReasonFilter, gateProfileFilter, outcomeFilter, pageLimit, proxyFilter]);

  const exitReasons = Array.from(
    new Set([
      ...trades.map((item) => item.exit_reason).filter((value): value is string => Boolean(value)),
      ...(exitReasonFilter ? [exitReasonFilter] : []),
    ]),
  ).sort();
  const entryRegimes = Array.from(
    new Set([
      ...trades.map((item) => item.entry_regime).filter((value): value is string => Boolean(value)),
      ...(entryRegimeFilter ? [entryRegimeFilter] : []),
    ]),
  ).sort();
  const entryLanes = Array.from(
    new Set([
      ...trades.map((item) => item.entry_lane).filter((value): value is string => Boolean(value)),
      ...(entryLaneFilter ? [entryLaneFilter] : []),
    ]),
  ).sort();
  const gateProfiles = Array.from(
    new Set([
      ...trades.map((item) => item.gate_profile).filter((value): value is string => Boolean(value)),
      ...(gateProfileFilter ? [gateProfileFilter] : []),
    ]),
  ).sort();
  const dexIds = Array.from(
    new Set([
      ...trades.map((item) => item.buy_dex_id).filter((value): value is string => Boolean(value)),
      ...(dexFilter ? [dexFilter] : []),
    ]),
  ).sort();

  const outcomeBreakdown = countBy(trades, (item) => item.outcome || "unknown");
  const exitBreakdown = countBy(trades, (item) => item.exit_reason || "unknown");
  const regimeBreakdown = countBy(trades, (item) => item.entry_regime || "unknown");
  const laneBreakdown = countBy(trades, (item) => item.entry_lane || "unknown");
  const topOutcomeCount = maxCount(outcomeBreakdown);
  const topExitCount = maxCount(exitBreakdown);
  const topRegimeCount = maxCount(regimeBreakdown);

  const winRate = summary?.win_rate_pct ?? null;
  const avgPnlPct = summary?.avg_pnl_pct ?? null;
  const totalPnlUsd = summary?.total_pnl_usd ?? null;

  function clearFilters() {
    setOutcomeFilter("all");
    setExitReasonFilter("");
    setEntryRegimeFilter("");
    setEntryLaneFilter("");
    setGateProfileFilter("");
    setDexFilter("");
    setProxyFilter("");
    resetPagination();
  }

  function resetPagination() {
    setCursorHistory([{ before_id: null, before_ts: null }]);
    setCursorIndex(0);
  }

  function applySavedView(filters: Record<string, unknown>) {
    setOutcomeFilter(typeof filters.outcomeFilter === "string" ? filters.outcomeFilter : "all");
    setExitReasonFilter(typeof filters.exitReasonFilter === "string" ? filters.exitReasonFilter : "");
    setEntryRegimeFilter(typeof filters.entryRegimeFilter === "string" ? filters.entryRegimeFilter : "");
    setEntryLaneFilter(typeof filters.entryLaneFilter === "string" ? filters.entryLaneFilter : "");
    setGateProfileFilter(typeof filters.gateProfileFilter === "string" ? filters.gateProfileFilter : "");
    setDexFilter(typeof filters.dexFilter === "string" ? filters.dexFilter : "");
    setProxyFilter(typeof filters.proxyFilter === "string" ? filters.proxyFilter : "");
    resetPagination();
  }

  function openReplay(tradeId: number) {
    navigate(`/trades/${tradeId}`);
  }

  function goNextPage() {
    if (!hasMore || !nextBeforeTs || !nextBeforeId) {
      return;
    }
    const nextCursor = { before_id: nextBeforeId, before_ts: nextBeforeTs };
    setCursorHistory((current) => {
      const base = current.slice(0, cursorIndex + 1);
      const tail = base[cursorIndex + 1];
      if (tail && tail.before_id === nextCursor.before_id && tail.before_ts === nextCursor.before_ts) {
        return base;
      }
      return [...base, nextCursor];
    });
    setCursorIndex((current) => current + 1);
  }

  function goPrevPage() {
    setCursorIndex((current) => Math.max(current - 1, 0));
  }

  function tradeColumns(): DataColumn<ClosedTradeItem>[] {
    return [
      {
        id: "trade",
        header: "Trade",
        render: (row) => (
          <button className="mono-link-button table-primary-cell" onClick={() => openReplay(row.trade_id)} type="button">
            <strong>{row.symbol || "Unknown"}</strong>
            <small>#{row.trade_id} | {row.address || "n/a"}</small>
          </button>
        ),
      },
      {
        id: "closed",
        header: "Closed",
        render: (row) => formatTimestamp(row.closed_at),
      },
      {
        id: "outcome",
        header: "Outcome",
        render: (row) => <StatusChip compact label={row.outcome || "unknown"} mono tone={outcomeTone(row.outcome)} />,
      },
      {
        id: "exit",
        header: "Exit reason",
        render: (row) => row.exit_reason || "n/a",
      },
      {
        id: "regime",
        header: "Regime",
        render: (row) => row.entry_regime || "n/a",
      },
      {
        id: "lane",
        header: "Lane",
        render: (row) => (
          <div className="table-primary-cell">
            <strong>{row.entry_lane || "n/a"}</strong>
            <small>{row.gate_profile || "no gate"}</small>
          </div>
        ),
      },
      {
        id: "dex",
        header: "Dex / proxy",
        render: (row) => (
          <div className="table-primary-cell">
            <strong>{row.buy_dex_id || "n/a"}</strong>
            <small>{row.buy_liquidity_is_proxy ? "proxy liquidity" : "real liquidity"}</small>
          </div>
        ),
      },
      {
        id: "peak",
        align: "right",
        header: "Peak",
        render: (row) => formatSignedPct(row.max_pnl_pct_seen ?? row.highest_pnl_pct),
      },
      {
        id: "pnlPct",
        align: "right",
        header: "PnL %",
        render: (row) => formatSignedPct(row.total_pnl_pct),
      },
      {
        id: "pnlUsd",
        align: "right",
        header: "PnL USD",
        render: (row) => formatUsd(row.total_pnl_usd),
      },
      {
        id: "buySol",
        align: "right",
        header: "Buy SOL",
        render: (row) => formatDecimal(row.buy_amount_sol, " SOL"),
      },
    ];
  }

  if (!tradesQuery.envelope && !tradesQuery.error) {
    return (
      <Surface eyebrow="Inspect / trades" title="Closed trades" subtitle="Waiting for the first closed trades payload">
        <p>The page is polling `/api/v1/trades/closed`.</p>
      </Surface>
    );
  }

  return (
    <div className="page-stack">
      <PageHero
        eyebrow="Inspect / closed loop"
        meta={
          <>
            <StatusChip
              label={tradesQuery.envelope?.meta.degraded ? "ledger degraded" : "ledger live"}
              tone={tradesQuery.envelope?.meta.degraded ? "warn" : "success"}
            />
            <StatusChip label={`${formatCount(totalCount)} total`} tone="info" compact />
            <StatusChip label={`${formatCount(pageCount)} on page`} tone="neutral" compact />
            <StatusChip label={`win rate ${formatDecimal(winRate)}%`} tone="neutral" compact />
          </>
        }
        question="How has the system been closing, and where is edge actually landing?"
        summary="Trades now exposes the closed ledger with outcome, exit and regime filters, plus direct jumps into replay for any historical row."
        title="Closed trades"
      />

      {consistency && !consistency.is_consistent ? (
        <Banner
          detail={`DB=${formatCount(consistency.db_closed_rows)} | paper=${formatCount(consistency.paper_closed_rows)} | scorecard=${formatCount(consistency.scorecard_live_closed)} | lag=${formatCount(consistency.lag_rows)}`}
          title="Ledger consistency drift"
          tone="warn"
        />
      ) : null}

      {tradesQuery.envelope?.meta.degraded ? (
        <Banner
          detail="The closed trade ledger is readable but at least one SQLite source is degraded. Use the provenance strip before treating this view as complete."
          title="Trades degraded"
          tone="warn"
        />
      ) : null}

      {tradesQuery.envelope?.meta.empty ? (
        <Banner
          detail="No closed trades match the active filter set. Clear filters or wait for new closed rows to appear."
          title="No closed trades"
          tone="info"
        />
      ) : null}

      {tradesQuery.error ? (
        <Banner
          detail={tradesQuery.error}
          title="Trades query failed"
          tone="danger"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface
          className="grid-span-12"
          eyebrow="Filters"
          title="Trade ledger controls"
          subtitle="Outcome, exit reason, regime and cursor pagination are mapped directly to the backend query contract."
          actions={
            <div className="page-hero__actions">
              <label className="filter-field filter-field--inline">
                <span>Page size</span>
                <select className="ui-field" onChange={(event) => setPageLimit(Number(event.target.value))} value={pageLimit}>
                  {pageSizeOptions.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </label>
              <button className="ui-button ui-button--ghost" onClick={clearFilters} type="button">
                Clear filters
              </button>
            </div>
          }
        >
          <div className="filter-stack">
            <div className="filter-field">
              <span>Outcome</span>
              <div className="choice-row">
                {outcomeOptions.map((option) => (
                  <button
                    className={["choice-chip", outcomeFilter === option ? "choice-chip--active" : ""].filter(Boolean).join(" ")}
                    key={option}
                    onClick={() => setOutcomeFilter(option)}
                    type="button"
                  >
                    {option}
                  </button>
                ))}
              </div>
            </div>

            <div className="filter-row">
              <label className="filter-field">
                <span>Exit reason</span>
                <select className="ui-field" onChange={(event) => setExitReasonFilter(event.target.value)} value={exitReasonFilter}>
                  <option value="">all</option>
                  {exitReasons.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>

              <label className="filter-field">
                <span>Entry regime</span>
                <select className="ui-field" onChange={(event) => setEntryRegimeFilter(event.target.value)} value={entryRegimeFilter}>
                  <option value="">all</option>
                  {entryRegimes.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>

              <label className="filter-field">
                <span>Entry lane</span>
                <select className="ui-field" onChange={(event) => setEntryLaneFilter(event.target.value)} value={entryLaneFilter}>
                  <option value="">all</option>
                  {entryLanes.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>

              <label className="filter-field">
                <span>Gate profile</span>
                <select className="ui-field" onChange={(event) => setGateProfileFilter(event.target.value)} value={gateProfileFilter}>
                  <option value="">all</option>
                  {gateProfiles.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>

              <label className="filter-field">
                <span>Buy dex</span>
                <select className="ui-field" onChange={(event) => setDexFilter(event.target.value)} value={dexFilter}>
                  <option value="">all</option>
                  {dexIds.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>

              <label className="filter-field">
                <span>Liquidity proxy</span>
                <select className="ui-field" onChange={(event) => setProxyFilter(event.target.value)} value={proxyFilter}>
                  <option value="">all</option>
                  <option value="real">real</option>
                  <option value="proxy">proxy</option>
                </select>
              </label>
            </div>

            <SavedViewsToolbar
              currentFilters={{ outcomeFilter, exitReasonFilter, entryRegimeFilter, entryLaneFilter, gateProfileFilter, dexFilter, proxyFilter }}
              onApply={applySavedView}
              pageKey="trades"
            />
          </div>
        </Surface>

        <Surface className="grid-span-8" eyebrow="Ledger summary" title="Historical posture">
          <div className="metric-ribbon">
            <div className="metric-ribbon__item">
              <span>Closed trades</span>
              <strong>{formatCount(summary?.closed_count)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Win rate</span>
              <strong>{formatDecimal(winRate)}%</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Average PnL</span>
              <strong>{formatSignedPct(avgPnlPct)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Median PnL</span>
              <strong>{formatSignedPct(summary?.median_pnl_pct)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Total PnL</span>
              <strong>{formatUsd(totalPnlUsd)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Latest close</span>
              <strong>{formatTimestamp(summary?.latest_closed_at)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Source truth" title="Ledger provenance">
          <SourceHealthStrip sources={sourceStatus} />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Current page" title="Wins and failures" subtitle="Breakdown over visible rows only. Totals above reflect the full filtered ledger.">
          <div className="breakdown-list">
            {outcomeBreakdown.map((row) => (
              <div className="breakdown-list__item" key={row.group}>
                <div className="breakdown-list__label">
                  <strong>{row.group}</strong>
                  <span>{formatCount(row.count)}</span>
                </div>
                <div className="breakdown-list__bar">
                  <span style={{ width: `${topOutcomeCount ? (row.count / topOutcomeCount) * 100 : 0}%` }} />
                </div>
              </div>
            ))}
            {!outcomeBreakdown.length ? <p className="empty-note">No outcomes to summarize.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Current page" title="Reasons to close" subtitle="Breakdown over visible rows only.">
          <div className="breakdown-list">
            {exitBreakdown.slice(0, 8).map((row) => (
              <div className="breakdown-list__item" key={row.group}>
                <div className="breakdown-list__label">
                  <strong>{row.group}</strong>
                  <span>{formatCount(row.count)}</span>
                </div>
                <div className="breakdown-list__bar breakdown-list__bar--warn">
                  <span style={{ width: `${topExitCount ? (row.count / topExitCount) * 100 : 0}%` }} />
                </div>
              </div>
            ))}
            {!exitBreakdown.length ? <p className="empty-note">No exit reasons to summarize.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Current page" title="Where edge lands" subtitle="Breakdown over visible rows only.">
          <div className="breakdown-list">
            {regimeBreakdown.slice(0, 8).map((row) => (
              <div className="breakdown-list__item" key={row.group}>
                <div className="breakdown-list__label">
                  <strong>{row.group}</strong>
                  <span>{formatCount(row.count)}</span>
                </div>
                <div className="breakdown-list__bar">
                  <span style={{ width: `${topRegimeCount ? (row.count / topRegimeCount) * 100 : 0}%` }} />
                </div>
              </div>
            ))}
            {!regimeBreakdown.length ? <p className="empty-note">No regime distribution to summarize.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Current page" title="Entry lanes" subtitle="Breakdown over visible rows only.">
          <div className="breakdown-list">
            {laneBreakdown.slice(0, 8).map((row) => (
              <div className="breakdown-list__item" key={row.group}>
                <div className="breakdown-list__label">
                  <strong>{row.group}</strong>
                  <span>{formatCount(row.count)}</span>
                </div>
              </div>
            ))}
            {!laneBreakdown.length ? <p className="empty-note">No lane distribution to summarize.</p> : null}
          </div>
        </Surface>

        <Surface
          className="grid-span-12"
          eyebrow="Closed ledger"
          title="Trades table"
          subtitle="Every row opens the full replay with runtime and research timelines."
          actions={
            <div className="page-hero__actions">
              <StatusChip label={`page ${cursorIndex + 1}`} tone="neutral" compact />
              <StatusChip label={`${formatCount(pageCount)} / ${formatCount(totalCount)}`} tone="info" compact />
              <button className="ui-button ui-button--ghost" disabled={cursorIndex === 0} onClick={goPrevPage} type="button">
                Previous
              </button>
              <button className="ui-button ui-button--ghost" disabled={!hasMore} onClick={goNextPage} type="button">
                Next
              </button>
            </div>
          }
        >
          <DataTable
            columns={tradeColumns()}
            emptyMessage="No closed trades available."
            rowKey={(row) => String(row.trade_id)}
            rows={trades}
          />
        </Surface>
      </div>
    </div>
  );
}
