import { Banner } from "../components/primitives/Banner";
import { DataTable, type DataColumn } from "../components/primitives/DataTable";
import { PageHero } from "../components/primitives/PageHero";
import { SourceHealthStrip } from "../components/primitives/SourceHealthStrip";
import { StatusChip } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type {
  HotQueueData,
  MissedPumpItem,
  MissedPumpsData,
  SniperStatusData,
  SocialsSummaryData,
} from "../lib/api";
import { formatCount, formatDecimal, formatSignedPct, formatTimestamp, formatUsd, humanizeKey } from "../lib/format";


interface ReasonRow {
  reason: string;
  count: number;
}


function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}


function boolLabel(value: unknown) {
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  if (value === null || value === undefined) {
    return "n/a";
  }
  return String(value);
}


function boolTone(value: unknown): "success" | "warn" | "neutral" {
  if (value === true) {
    return "success";
  }
  if (value === false) {
    return "warn";
  }
  return "neutral";
}


function numericValue(value: unknown) {
  return typeof value === "number" ? value : null;
}


function reasonRows(value: Record<string, number> | Array<[string, number]> | undefined): ReasonRow[] {
  if (!value) {
    return [];
  }
  if (Array.isArray(value)) {
    return value.map(([reason, count]) => ({ reason, count })).sort((left, right) => right.count - left.count);
  }
  return Object.entries(value)
    .map(([reason, count]) => ({ reason, count }))
    .sort((left, right) => right.count - left.count || left.reason.localeCompare(right.reason));
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


const reasonColumns: DataColumn<ReasonRow>[] = [
  {
    id: "reason",
    header: "Reason",
    mono: true,
    render: (row) => row.reason,
  },
  {
    id: "count",
    align: "right",
    header: "Count",
    render: (row) => formatCount(row.count),
  },
];


const missedColumns: DataColumn<MissedPumpItem>[] = [
  {
    id: "token",
    header: "Token",
    render: (row) => (
      <div className="table-primary-cell">
        <strong>{row.symbol || "Unknown"}</strong>
        <small>{row.address || "n/a"}</small>
      </div>
    ),
  },
  {
    id: "seen",
    header: "First seen",
    render: (row) => formatTimestamp(row.first_seen_at),
  },
  {
    id: "price5m",
    align: "right",
    header: "Price 5m",
    render: (row) => formatSignedPct(numericValue(row.price_pct_5m_at_seen)),
  },
  {
    id: "txns",
    align: "right",
    header: "Txns 5m",
    render: (row) => formatCount(numericValue(row.txns_5m_at_seen)),
  },
  {
    id: "liq",
    align: "right",
    header: "Liquidity",
    render: (row) => formatUsd(numericValue(row.liquidity_at_seen)),
  },
  {
    id: "blocked",
    header: "Rule blocked",
    mono: true,
    render: (row) => row.rule_that_blocked || row.reject_reason || row.delay_reason || row.shadow_reason || "unknown",
  },
  {
    id: "later",
    align: "right",
    header: "Later peak",
    render: (row) => formatSignedPct(numericValue(row.later_max_pnl_pct)),
  },
];


export function SniperPage() {
  const statusQuery = usePollEnvelope<SniperStatusData>("/api/v1/sniper/status", 5000);
  const missedQuery = usePollEnvelope<MissedPumpsData>("/api/v1/sniper/missed-pumps?limit=25", 30000);
  const hotQueueQuery = usePollEnvelope<HotQueueData>("/api/v1/sniper/hot-queue", 5000);
  const socialsQuery = usePollEnvelope<SocialsSummaryData>("/api/v1/sniper/socials-summary", 30000);

  const status = statusQuery.envelope?.data;
  const missed = missedQuery.envelope?.data;
  const hotQueue = hotQueueQuery.envelope?.data || status?.hot_queue || {};
  const socials = socialsQuery.envelope?.data;
  const policy = asRecord(status?.green_sniper_policy);
  const liveCanary = asRecord(status?.live_canary);
  const queryError = statusQuery.error || missedQuery.error || hotQueueQuery.error || socialsQuery.error;
  const sources = [
    ...(statusQuery.envelope?.meta.source_status || []),
    ...(missedQuery.envelope?.meta.source_status || []),
    ...(hotQueueQuery.envelope?.meta.source_status || []),
    ...(socialsQuery.envelope?.meta.source_status || []),
  ].filter((item, index, items) => items.findIndex((other) => other.source_key === item.source_key) === index);

  const rejects = reasonRows(status?.green_sniper_rejects_today);
  const shadows = reasonRows(status?.green_sniper_shadows_today);
  const missedRows = missed?.items || status?.missed_pumps_top10 || [];

  if (!status && !queryError) {
    return (
      <Surface eyebrow="Monitor / sniper" title="Sniper status" subtitle="Waiting for sniper telemetry">
        <p>The page is polling `/api/v1/sniper/status`, `/api/v1/sniper/missed-pumps`, and `/api/v1/sniper/hot-queue`.</p>
      </Surface>
    );
  }

  return (
    <div className="page-stack">
      <PageHero
        eyebrow="Monitor / green sniper"
        meta={
          <>
            <StatusChip
              label={statusQuery.envelope?.meta.degraded ? "sniper degraded" : "sniper telemetry"}
              tone={statusQuery.envelope?.meta.degraded ? "warn" : "success"}
            />
            <StatusChip label={`hot queue ${formatCount(status?.hot_queue_size ?? numericValue(hotQueue.size))}`} tone="info" compact />
            <StatusChip label={policy?.live_enabled ? "live canary on" : "live canary off"} tone={policy?.live_enabled ? "warn" : "neutral"} compact />
            <StatusChip label={policy?.paper_sniper_mode ? "paper sniper" : "standard paper"} tone={policy?.paper_sniper_mode ? "success" : "neutral"} compact />
          </>
        }
        question="Is the bot actually seeing, evaluating, buying, shadowing, or missing hot newborn pumps?"
        summary="This page surfaces the green-sniper lane directly: hot queue pressure, latency to eval/buy, rejection reasons, missed pumps, live canary limits, and the active sniper policy."
        title="Sniper status"
      />

      {queryError ? (
        <Banner detail={queryError} title="Sniper query failed" tone="danger" />
      ) : null}

      {policy?.allow_proxy_liquidity_paper ? (
        <Banner
          detail="Paper green sniper is allowed to use proxy liquidity. This is useful for dataset acquisition but should be interpreted separately from strict PnL validation."
          title="Proxy liquidity enabled"
          tone="warn"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface className="grid-span-8" eyebrow="Sniper funnel" title="Hot path posture">
          <div className="metric-ribbon">
            <div className="metric-ribbon__item">
              <span>Hot queue size</span>
              <strong>{formatCount(status?.hot_queue_size ?? numericValue(hotQueue.size))}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Green buys</span>
              <strong>{formatCount(status?.green_sniper_buys_today)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Reject reasons</span>
              <strong>{formatCount(rejects.length)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Shadow reasons</span>
              <strong>{formatCount(shadows.length)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Avg seen to eval</span>
              <strong>{formatDecimal(status?.avg_time_to_eval_s, "s")}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Avg seen to buy</span>
              <strong>{formatDecimal(status?.avg_time_to_buy_s, "s")}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Source truth" title="Sniper provenance">
          <SourceHealthStrip sources={sources} />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Policy" title="Green sniper lane">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Enabled</span>
              <strong>{boolLabel(policy?.enabled)}</strong>
            </div>
            <div className="kv-cell">
              <span>Paper sniper</span>
              <strong>{boolLabel(policy?.paper_sniper_mode)}</strong>
            </div>
            <div className="kv-cell">
              <span>Route paper</span>
              <strong>{boolLabel(policy?.require_route_paper)}</strong>
            </div>
            <div className="kv-cell">
              <span>Route live</span>
              <strong>{boolLabel(policy?.require_route_live)}</strong>
            </div>
            <div className="kv-cell">
              <span>Proxy paper</span>
              <strong>{boolLabel(policy?.allow_proxy_liquidity_paper)}</strong>
            </div>
            <div className="kv-cell">
              <span>ML mode</span>
              <strong>{String(policy?.ml_mode || "n/a")}</strong>
            </div>
            <div className="kv-cell">
              <span>ML can block</span>
              <strong>{boolLabel(policy?.ml_can_block)}</strong>
            </div>
            <div className="kv-cell">
              <span>Rank guard min</span>
              <strong>{formatDecimal(numericValue(policy?.rank_guard_min_score))}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Social intelligence" title="Coverage and risk flags">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Rows</span>
              <strong>{formatCount(socials?.rows)}</strong>
            </div>
            <div className="kv-cell">
              <span>Unique tokens</span>
              <strong>{formatCount(socials?.unique_tokens)}</strong>
            </div>
            <div className="kv-cell">
              <span>Coverage</span>
              <strong>{formatDecimal(socials?.coverage_pct, "%")}</strong>
            </div>
            <div className="kv-cell">
              <span>Present</span>
              <strong>{formatCount(socials?.status_counts?.present)}</strong>
            </div>
            <div className="kv-cell">
              <span>Missing</span>
              <strong>{formatCount(socials?.status_counts?.missing)}</strong>
            </div>
            <div className="kv-cell">
              <span>Suspicious</span>
              <strong>{formatCount(socials?.status_counts?.suspicious)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Live canary" title="Entry safety limits">
          <div className="kv-grid">
            {Object.entries(liveCanary || {}).slice(0, 10).map(([key, value]) => (
              <div className="kv-cell" key={key}>
                <span>{humanizeKey(key)}</span>
                <strong>{stringifyValue(value)}</strong>
              </div>
            ))}
            {!liveCanary ? <p className="empty-note">No live canary snapshot available.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Hot queue" title="Raw queue snapshot">
          <div className="kv-grid">
            {Object.entries(hotQueue || {}).slice(0, 10).map(([key, value]) => (
              <div className="kv-cell" key={key}>
                <span>{humanizeKey(key)}</span>
                <strong>{stringifyValue(value)}</strong>
              </div>
            ))}
          </div>
        </Surface>

        <Surface className="grid-span-6" eyebrow="Rejects" title="Top green sniper reject reasons">
          <DataTable
            columns={reasonColumns}
            emptyMessage="No green sniper rejects have been recorded."
            rowKey={(row) => row.reason}
            rows={rejects.slice(0, 12)}
          />
        </Surface>

        <Surface className="grid-span-6" eyebrow="Shadows" title="Shadowed hot candidates">
          <DataTable
            columns={reasonColumns}
            emptyMessage="No green sniper shadow reasons have been recorded."
            rowKey={(row) => row.reason}
            rows={shadows.slice(0, 12)}
          />
        </Surface>

        <Surface className="grid-span-12" eyebrow="Missed pumps" title="Largest missed opportunities" subtitle="Any hot candidate that later moved hard should name the rule that blocked it.">
          <DataTable
            columns={missedColumns}
            emptyMessage="No missed pump report rows available."
            rowKey={(row) => String(row.address || row.symbol || row.first_seen_at)}
            rows={missedRows}
          />
        </Surface>

        <Surface className="grid-span-12" eyebrow="Contract" title="Green sniper guardrails">
          <div className="strategy-grid">
            {[
              { label: "Socials", value: "never hard gate green sniper", tone: "success" as const },
              { label: "ML", value: policy?.ml_can_block ? "can block" : "copilot only", tone: policy?.ml_can_block ? "warn" as const : "success" as const },
              { label: "Paper proxy", value: boolLabel(policy?.allow_proxy_liquidity_paper), tone: boolTone(policy?.allow_proxy_liquidity_paper) },
              { label: "Live route", value: boolLabel(policy?.require_route_live), tone: boolTone(policy?.require_route_live) },
            ].map((item) => (
              <div className="strategy-card" key={item.label}>
                <div className="strategy-card__header">
                  <strong>{item.label}</strong>
                  <StatusChip label={item.value} tone={item.tone} compact />
                </div>
              </div>
            ))}
          </div>
        </Surface>
      </div>
    </div>
  );
}
