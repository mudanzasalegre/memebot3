import { useDeferredValue, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Banner } from "../components/primitives/Banner";
import { DataTable, type DataColumn } from "../components/primitives/DataTable";
import { PageHero } from "../components/primitives/PageHero";
import { SourceHealthStrip } from "../components/primitives/SourceHealthStrip";
import { StatusChip } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type { OpenPositionItem, OpenPositionsData } from "../lib/api";
import { formatCount, formatDecimal, formatRelative, formatSignedPct, formatTimestamp, formatUsd } from "../lib/format";


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


function sum(values: Array<number | null | undefined>) {
  return values.reduce<number>((total, value) => total + (value ?? 0), 0);
}


function average(values: Array<number | null | undefined>) {
  const valid = values.filter((value): value is number => value !== null && value !== undefined);
  if (!valid.length) {
    return null;
  }
  return valid.reduce((total, value) => total + value, 0) / valid.length;
}


function countBy(items: OpenPositionItem[], key: (item: OpenPositionItem) => string) {
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


export function PositionsPage() {
  const navigate = useNavigate();
  const [addressFilter, setAddressFilter] = useState("");
  const deferredAddress = useDeferredValue(addressFilter.trim());

  const positionsQuery = usePollEnvelope<OpenPositionsData>(
    buildPath("/api/v1/positions/open", {
      limit: 50,
      address: deferredAddress || undefined,
    }),
    5000,
  );

  const positions = positionsQuery.envelope?.data.items || [];
  const sourceStatus = positionsQuery.envelope?.meta.source_status || [];
  const regimeBreakdown = countBy(positions, (item) => item.entry_regime || "unknown");
  const sizeBreakdown = countBy(positions, (item) => item.size_bucket || "unknown");
  const topRegimeCount = maxCount(regimeBreakdown);
  const topSizeCount = maxCount(sizeBreakdown);

  const totalBuySol = sum(positions.map((item) => item.buy_amount_sol));
  const avgEntryAi = average(positions.map((item) => item.entry_ai_proba));
  const avgPeakPnl = average(positions.map((item) => item.highest_pnl_pct));
  const avgLiquidity = average(positions.map((item) => item.buy_liquidity_usd));

  function clearFilters() {
    setAddressFilter("");
  }

  function openReplay(tradeId: number) {
    navigate(`/trades/${tradeId}`);
  }

  function positionColumns(): DataColumn<OpenPositionItem>[] {
    return [
      {
        id: "token",
        header: "Token",
        render: (row) => (
          <button className="mono-link-button table-primary-cell" onClick={() => openReplay(row.trade_id)} type="button">
            <strong>{row.symbol || "Unknown"}</strong>
            <small>#{row.trade_id} | {row.address || "n/a"}</small>
          </button>
        ),
      },
      {
        id: "opened",
        header: "Opened",
        render: (row) => formatTimestamp(row.opened_at),
      },
      {
        id: "regime",
        header: "Regime",
        render: (row) => row.entry_regime || "n/a",
      },
      {
        id: "buySol",
        align: "right",
        header: "Buy SOL",
        render: (row) => formatDecimal(row.buy_amount_sol, " SOL"),
      },
      {
        id: "liquidity",
        align: "right",
        header: "Liquidity",
        render: (row) => formatUsd(row.buy_liquidity_usd),
      },
      {
        id: "entryAi",
        align: "right",
        header: "Entry AI",
        render: (row) => formatDecimal(row.entry_ai_proba),
      },
      {
        id: "peak",
        align: "right",
        header: "Peak PnL",
        render: (row) => formatSignedPct(row.highest_pnl_pct),
      },
    ];
  }

  if (!positionsQuery.envelope && !positionsQuery.error) {
    return (
      <Surface eyebrow="Inspect / positions" title="Open positions" subtitle="Waiting for the first open positions payload">
        <p>The page is polling `/api/v1/positions/open`.</p>
      </Surface>
    );
  }

  return (
    <div className="page-stack">
      <PageHero
        eyebrow="Inspect / open risk"
        meta={
          <>
            <StatusChip
              label={positionsQuery.envelope?.meta.degraded ? "positions degraded" : "positions live"}
              tone={positionsQuery.envelope?.meta.degraded ? "warn" : "success"}
            />
            <StatusChip label={`${formatCount(positionsQuery.envelope?.data.count)} open`} tone="info" compact />
            {deferredAddress ? <StatusChip label={`address ${deferredAddress}`} tone="info" compact mono /> : null}
          </>
        }
        question="How much open risk is live right now?"
        summary="Positions now exposes the live open inventory, exposure by regime and size, and direct jumps into replay without touching SQLite."
        title="Open positions"
      />

      {positionsQuery.envelope?.meta.degraded ? (
        <Banner
          detail="One of the SQLite sources behind open positions is degraded. The table remains readable, but it should not be treated as complete until source health recovers."
          title="Positions degraded"
          tone="warn"
        />
      ) : null}

      {positionsQuery.envelope?.meta.empty ? (
        <Banner
          detail="There are no open positions in the current dataset. The page stays valid and will populate automatically when open rows appear again."
          title="No open positions"
          tone="info"
        />
      ) : null}

      {positionsQuery.error ? (
        <Banner
          detail={positionsQuery.error}
          title="Positions query failed"
          tone="danger"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface
          className="grid-span-12"
          eyebrow="Filters"
          title="Open risk controls"
          subtitle="Filter by token address and drill into the replay from any live row."
          actions={
            <button className="ui-button ui-button--ghost" onClick={clearFilters} type="button">
              Clear filter
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
          </div>
        </Surface>

        <Surface className="grid-span-8" eyebrow="Exposure summary" title="Current open risk posture">
          <div className="metric-ribbon">
            <div className="metric-ribbon__item">
              <span>Open positions</span>
              <strong>{formatCount(positions.length)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Committed SOL</span>
              <strong>{formatDecimal(totalBuySol, " SOL")}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Average peak PnL</span>
              <strong>{formatSignedPct(avgPeakPnl)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Average entry AI</span>
              <strong>{formatDecimal(avgEntryAi)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Source truth" title="Positions provenance">
          <SourceHealthStrip sources={sourceStatus} />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Regime exposure" title="Open by regime">
          <div className="breakdown-list">
            {regimeBreakdown.map((row) => (
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
            {!regimeBreakdown.length ? <p className="empty-note">No regime exposure to show.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Size posture" title="Open by size bucket">
          <div className="breakdown-list">
            {sizeBreakdown.map((row) => (
              <div className="breakdown-list__item" key={row.group}>
                <div className="breakdown-list__label">
                  <strong>{row.group}</strong>
                  <span>{formatCount(row.count)}</span>
                </div>
                <div className="breakdown-list__bar">
                  <span style={{ width: `${topSizeCount ? (row.count / topSizeCount) * 100 : 0}%` }} />
                </div>
              </div>
            ))}
            {!sizeBreakdown.length ? <p className="empty-note">No size bucket exposure to show.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Entry quality" title="Context at buy">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Average liquidity</span>
              <strong>{formatUsd(avgLiquidity)}</strong>
            </div>
            <div className="kv-cell">
              <span>Average score</span>
              <strong>{formatDecimal(average(positions.map((item) => item.entry_score_total)))}</strong>
            </div>
            <div className="kv-cell">
              <span>Average market cap</span>
              <strong>{formatUsd(average(positions.map((item) => item.buy_market_cap_usd)))}</strong>
            </div>
            <div className="kv-cell">
              <span>Newest open</span>
              <strong>{positions[0]?.opened_at ? `${formatRelative(positions[0].opened_at)} ago` : "n/a"}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-12" eyebrow="Open inventory" title="Live positions table" subtitle="Every row links straight into trade replay.">
          <DataTable
            columns={positionColumns()}
            emptyMessage="No open positions available."
            rowKey={(row) => String(row.trade_id)}
            rows={positions}
          />
        </Surface>
      </div>
    </div>
  );
}
