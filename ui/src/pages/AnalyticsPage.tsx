import { DataTable, type DataColumn } from "../components/primitives/DataTable";
import { Banner } from "../components/primitives/Banner";
import { PageHero } from "../components/primitives/PageHero";
import { SourceHealthStrip } from "../components/primitives/SourceHealthStrip";
import { StatusChip } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type {
  AnalyticsBaselineData,
  AnalyticsCoverageRow,
  AnalyticsEdgeData,
  AnalyticsGroupRow,
} from "../lib/api";
import { formatCount, formatDecimal, formatSignedPct, formatTimestamp, formatUsd, humanizeKey } from "../lib/format";


function stringifyValue(value: unknown) {
  if (value === null || value === undefined) {
    return "n/a";
  }
  if (Array.isArray(value)) {
    return value.join(", ");
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


function maxCount(rows: Array<{ count: number }>) {
  return rows.reduce((current, row) => Math.max(current, row.count), 0);
}


function groupColumns(groupLabel = "Group"): DataColumn<AnalyticsGroupRow>[] {
  return [
    {
      id: "group",
      header: groupLabel,
      render: (row) => row.group,
    },
    {
      id: "count",
      align: "right",
      header: "Count",
      render: (row) => formatCount(row.count),
    },
    {
      id: "win",
      align: "right",
      header: "Win rate",
      render: (row) => formatDecimal(row.win_rate_pct),
    },
    {
      id: "pnl",
      align: "right",
      header: "Avg PnL",
      render: (row) => formatSignedPct(row.avg_pnl_pct),
    },
    {
      id: "hold",
      align: "right",
      header: "Avg hold",
      render: (row) => formatDecimal(row.avg_hold_minutes, "m"),
    },
  ];
}


const coverageColumns: DataColumn<AnalyticsCoverageRow>[] = [
  {
    id: "field",
    header: "Field",
    render: (row) => row.field,
  },
  {
    id: "presentCount",
    align: "right",
    header: "Present",
    render: (row) => formatCount(row.present_count),
  },
  {
    id: "presentPct",
    align: "right",
    header: "Present %",
    render: (row) => formatDecimal(row.present_pct, "%"),
  },
];


export function AnalyticsPage() {
  const baselineQuery = usePollEnvelope<AnalyticsBaselineData>("/api/v1/analytics/baseline", 60000);
  const edgeQuery = usePollEnvelope<AnalyticsEdgeData>("/api/v1/analytics/edge", 60000);

  const baseline = baselineQuery.envelope?.data;
  const edge = edgeQuery.envelope?.data;
  const consistency = edge?.consistency || baseline?.consistency;
  const sourceStatus = [
    ...(baselineQuery.envelope?.meta.source_status || []),
    ...(edgeQuery.envelope?.meta.source_status || []),
  ].filter((status, index, items) => items.findIndex((item) => item.source_key === status.source_key) === index);
  const queryError = baselineQuery.error || edgeQuery.error;

  const exitRows = edge?.exit_reason || [];
  const regimeRows = edge?.regimes.entry_regime || [];
  const sizeRows = edge?.sizing.size_bucket || [];
  const topExitCount = maxCount(exitRows);
  const topRegimeCount = maxCount(regimeRows);
  const topSizeCount = maxCount(sizeRows);

  const nullPctRows = Object.entries(baseline?.features.null_pct || {})
    .map(([field, pct]) => ({ field, pct }))
    .sort((left, right) => right.pct - left.pct)
    .slice(0, 8);

  if (!baseline && !edge && !queryError) {
    return (
      <Surface eyebrow="Inspect / analytics" title="Analytics" subtitle="Waiting for the first analytics payloads">
        <p>The page is polling `/api/v1/analytics/baseline` and `/api/v1/analytics/edge`.</p>
      </Surface>
    );
  }

  return (
    <div className="page-stack">
      <PageHero
        eyebrow="Inspect / edge"
        meta={
          <>
            <StatusChip
              label={edgeQuery.envelope?.meta.degraded ? "edge degraded" : "edge ready"}
              tone={edgeQuery.envelope?.meta.degraded ? "warn" : "success"}
            />
            <StatusChip label={`${formatCount(edge?.overview.closed_trades)} closed`} tone="info" compact />
            <StatusChip label={`win ${formatDecimal(edge?.overview.win_rate_pct)}%`} tone="neutral" compact />
          </>
        }
        question="Where is edge concentrated, and where is it leaking away?"
        summary="Analytics now turns the reporting layer into an operator page: exit distribution, regime posture, sizing behavior, feature coverage and requeue aftermath sit in one place."
        title="Edge analytics"
      />

      {edgeQuery.envelope?.meta.empty ? (
        <Banner
          detail="The edge snapshot has no closed trades to analyze. Baseline and source truth still render so the operator sees why the page is empty."
          title="No edge snapshot"
          tone="info"
        />
      ) : null}

      {edgeQuery.envelope?.meta.degraded || baselineQuery.envelope?.meta.degraded ? (
        <Banner
          detail="One of the analytics sources is degraded. The page remains usable, but edge and baseline should not be treated as fully complete."
          title="Analytics degraded"
          tone="warn"
        />
      ) : null}

      {consistency && !consistency.is_consistent ? (
        <Banner
          detail={`DB=${formatCount(consistency.db_closed_rows)} | paper=${formatCount(consistency.paper_closed_rows)} | scorecard=${formatCount(consistency.scorecard_live_closed)} | lag=${formatCount(consistency.lag_rows)}`}
          title="Live vs derived drift"
          tone="warn"
        />
      ) : null}

      {queryError ? (
        <Banner
          detail={queryError}
          title="Analytics query failed"
          tone="danger"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface className="grid-span-8" eyebrow="Overview" title="Project edge posture">
          <div className="metric-ribbon">
            <div className="metric-ribbon__item">
              <span>Closed trades</span>
              <strong>{formatCount(edge?.overview.closed_trades)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Win rate</span>
              <strong>{formatDecimal(edge?.overview.win_rate_pct)}%</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Average PnL</span>
              <strong>{formatSignedPct(edge?.overview.avg_pnl_pct)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Median PnL</span>
              <strong>{formatSignedPct(edge?.overview.median_pnl_pct)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Average giveback</span>
              <strong>{formatDecimal(edge?.overview.avg_giveback_pct)}%</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Source truth" title="Analytics provenance">
          <SourceHealthStrip sources={sourceStatus} />
        </Surface>

        <Surface className="grid-span-12" eyebrow="Consistency" title="Ledger and scorecard coherence">
          <div className="metric-ribbon">
            <div className="metric-ribbon__item">
              <span>DB closed</span>
              <strong>{formatCount(consistency?.db_closed_rows)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Paper closed</span>
              <strong>{formatCount(consistency?.paper_closed_rows)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Scorecard live closed</span>
              <strong>{formatCount(consistency?.scorecard_live_closed)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Lag rows</span>
              <strong>{formatCount(consistency?.lag_rows)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Latest closed</span>
              <strong>{formatTimestamp(consistency?.latest_closed_at)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Scorecard generated</span>
              <strong>{formatTimestamp(consistency?.scorecard_generated_at_utc)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-8" eyebrow="Exit distribution" title="Edge by exit reason">
          <DataTable
            columns={groupColumns("Exit reason")}
            emptyMessage="No exit distribution available."
            rowKey={(row) => row.group}
            rows={exitRows}
          />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Winners" title="Giveback posture">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Winning trades</span>
              <strong>{formatCount(edge?.winners.count)}</strong>
            </div>
            <div className="kv-cell">
              <span>Average giveback</span>
              <strong>{formatDecimal(edge?.winners.avg_giveback_pct)}%</strong>
            </div>
            <div className="kv-cell">
              <span>Median giveback</span>
              <strong>{formatDecimal(edge?.winners.median_giveback_pct)}%</strong>
            </div>
            <div className="kv-cell">
              <span>{"Giveback >=20%"}</span>
              <strong>{formatCount(edge?.winners.giveback_ge_20pct_count)}</strong>
            </div>
            <div className="kv-cell">
              <span>{"Giveback >=40%"}</span>
              <strong>{formatCount(edge?.winners.giveback_ge_40pct_count)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Regime spread" title="Edge by entry regime">
          <div className="breakdown-list">
            {regimeRows.map((row) => (
              <div className="breakdown-list__item" key={row.group}>
                <div className="breakdown-list__label">
                  <strong>{row.group}</strong>
                  <span>{formatSignedPct(row.avg_pnl_pct)}</span>
                </div>
                <div className="breakdown-list__bar">
                  <span style={{ width: `${topRegimeCount ? (row.count / topRegimeCount) * 100 : 0}%` }} />
                </div>
              </div>
            ))}
            {!regimeRows.length ? <p className="empty-note">No regime distribution available.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Sizing spread" title="Edge by size bucket">
          <div className="breakdown-list">
            {sizeRows.map((row) => (
              <div className="breakdown-list__item" key={row.group}>
                <div className="breakdown-list__label">
                  <strong>{row.group}</strong>
                  <span>{formatSignedPct(row.avg_pnl_pct)}</span>
                </div>
                <div className="breakdown-list__bar">
                  <span style={{ width: `${topSizeCount ? (row.count / topSizeCount) * 100 : 0}%` }} />
                </div>
              </div>
            ))}
            {!sizeRows.length ? <p className="empty-note">No size distribution available.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Requeues" title="Buys after backoff">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Runtime rows</span>
              <strong>{formatCount(edge?.requeues.rows)}</strong>
            </div>
            <div className="kv-cell">
              <span>Requeue rows</span>
              <strong>{formatCount(edge?.requeues.requeue_rows?.length)}</strong>
            </div>
            <div className="kv-cell">
              <span>Addresses requeued</span>
              <strong>{formatCount(edge?.requeues.addresses_requeued)}</strong>
            </div>
            <div className="kv-cell">
              <span>Bought after requeue</span>
              <strong>{formatCount(edge?.requeues.addresses_bought_after_requeue)}</strong>
            </div>
            <div className="kv-cell">
              <span>Minutes first seen to buy</span>
              <strong>{formatDecimal(edge?.requeues.avg_minutes_first_seen_to_buy, "m")}</strong>
            </div>
            <div className="kv-cell">
              <span>Avg requeues before buy</span>
              <strong>{formatDecimal(edge?.requeues.avg_requeues_before_buy)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Feature coverage" title="Column presence in parquet">
          <DataTable
            columns={coverageColumns}
            emptyMessage="No feature coverage available."
            rowKey={(row) => row.field}
            rows={edge?.coverage || []}
          />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Baseline dataset" title="Static baseline context">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Open rows</span>
              <strong>{formatCount(baseline?.positions.open_rows)}</strong>
            </div>
            <div className="kv-cell">
              <span>Closed rows</span>
              <strong>{formatCount(baseline?.positions.closed_rows)}</strong>
            </div>
            <div className="kv-cell">
              <span>Feature rows</span>
              <strong>{formatCount(baseline?.features.rows)}</strong>
            </div>
            <div className="kv-cell">
              <span>Unique tokens</span>
              <strong>{formatCount(baseline?.features.unique_tokens)}</strong>
            </div>
            <div className="kv-cell">
              <span>Positives</span>
              <strong>{formatCount(baseline?.features.positives)}</strong>
            </div>
            <div className="kv-cell">
              <span>Constant columns</span>
              <strong>{formatCount(baseline?.features.constant_columns.length)}</strong>
            </div>
          </div>
          <div className="breakdown-list">
            {nullPctRows.map((row) => (
              <div className="breakdown-list__item" key={row.field}>
                <div className="breakdown-list__label">
                  <strong>{row.field}</strong>
                  <span>{formatDecimal(row.pct, "% null")}</span>
                </div>
              </div>
            ))}
            {!nullPctRows.length ? <p className="empty-note">No null-percentage summary available.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-12" eyebrow="Execution sources" title="Buy and close source posture" subtitle={baseline?.project_root || undefined}>
          <div className="strategy-grid">
            <div className="strategy-card">
              <div className="strategy-card__header">
                <strong>Buy source</strong>
                <StatusChip label={edge?.price_sources_buy?.[0]?.group || "n/a"} tone="info" compact mono />
              </div>
              <div className="strategy-card__stats">
                <div>
                  <span>Rows</span>
                  <strong>{formatCount(edge?.price_sources_buy?.[0]?.count)}</strong>
                </div>
                <div>
                  <span>Avg PnL</span>
                  <strong>{formatSignedPct(edge?.price_sources_buy?.[0]?.avg_pnl_pct)}</strong>
                </div>
              </div>
            </div>
            <div className="strategy-card">
              <div className="strategy-card__header">
                <strong>Close source</strong>
                <StatusChip label={edge?.price_sources_close?.[0]?.group || "n/a"} tone="info" compact mono />
              </div>
              <div className="strategy-card__stats">
                <div>
                  <span>Rows</span>
                  <strong>{formatCount(edge?.price_sources_close?.[0]?.count)}</strong>
                </div>
                <div>
                  <span>Avg hold</span>
                  <strong>{formatDecimal(edge?.price_sources_close?.[0]?.avg_hold_minutes, "m")}</strong>
                </div>
              </div>
            </div>
            <div className="strategy-card">
              <div className="strategy-card__header">
                <strong>Source pair</strong>
                <StatusChip label={edge?.price_source_pairs?.[0]?.group || "n/a"} tone="neutral" compact mono />
              </div>
              <div className="strategy-card__stats">
                <div>
                  <span>Rows</span>
                  <strong>{formatCount(edge?.price_source_pairs?.[0]?.count)}</strong>
                </div>
                <div>
                  <span>Avg giveback</span>
                  <strong>{formatDecimal(edge?.price_source_pairs?.[0]?.avg_giveback_pct)}%</strong>
                </div>
              </div>
            </div>
          </div>
        </Surface>
      </div>
    </div>
  );
}
