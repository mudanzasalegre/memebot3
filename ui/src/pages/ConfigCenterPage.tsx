import { useDeferredValue, useState } from "react";

import { useDrawer } from "../app/drawer";
import { Banner } from "../components/primitives/Banner";
import { DataTable, type DataColumn } from "../components/primitives/DataTable";
import { PageHero } from "../components/primitives/PageHero";
import { SourceHealthStrip } from "../components/primitives/SourceHealthStrip";
import { StatusChip } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type { ConfigEffectiveData, ConfigPoliciesData } from "../lib/api";
import { formatCount, formatDecimal, humanizeKey } from "../lib/format";


interface ConfigRow {
  key: string;
  value: unknown;
}


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


function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}


const configColumns: DataColumn<ConfigRow>[] = [
  {
    id: "key",
    header: "Key",
    mono: true,
    render: (row) => row.key,
  },
  {
    id: "value",
    header: "Value",
    render: (row) => stringifyValue(row.value),
  },
];


export function ConfigCenterPage() {
  const { openPanel } = useDrawer();
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query.trim().toLowerCase());

  const effectiveQuery = usePollEnvelope<ConfigEffectiveData>("/api/v1/config/effective", 60000);
  const policiesQuery = usePollEnvelope<ConfigPoliciesData>("/api/v1/config/policies", 60000);

  const effective = effectiveQuery.envelope?.data;
  const policies = policiesQuery.envelope?.data;
  const sourceStatus = [
    ...(effectiveQuery.envelope?.meta.source_status || []),
    ...(policiesQuery.envelope?.meta.source_status || []),
  ].filter((item, index, items) => items.findIndex((other) => other.source_key === item.source_key) === index);
  const queryError = effectiveQuery.error || policiesQuery.error;

  const effectiveRows = Object.entries(effective || {})
    .filter(([key]) => !deferredQuery || key.toLowerCase().includes(deferredQuery))
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => ({ key, value }));

  const strategyPolicies = Object.entries(asRecord(policies?.strategy) || {}).map(([regime, value]) => ({
    regime,
    payload: asRecord(value),
  }));

  function openRawRecord(title: string, description: string, record: unknown) {
    openPanel({
      eyebrow: "Config / raw payload",
      title,
      description,
      content: (
        <div className="drawer-stack">
          <pre className="drawer-note">{JSON.stringify(record, null, 2)}</pre>
        </div>
      ),
    });
  }

  if (!effective && !policies && !queryError) {
    return (
      <Surface eyebrow="Inspect / config" title="Config Center" subtitle="Waiting for the first config payloads">
        <p>The page is polling `/api/v1/config/effective` and `/api/v1/config/policies`.</p>
      </Surface>
    );
  }

  return (
    <div className="page-stack">
      <PageHero
        eyebrow="Inspect / policy"
        meta={
          <>
            <StatusChip
              label={effectiveQuery.envelope?.meta.degraded ? "config degraded" : "config ready"}
              tone={effectiveQuery.envelope?.meta.degraded ? "warn" : "success"}
            />
            <StatusChip label={String(effective?.DRY_RUN ? "dry run" : "live capital")} tone={effective?.DRY_RUN ? "info" : "success"} compact />
            <StatusChip label={String(effective?.ML_GATE_MODE || "ml mode n/a")} tone="neutral" compact mono />
            <StatusChip label={`${formatCount(effectiveRows.length)} keys`} tone="neutral" compact />
          </>
        }
        question="Which policy is actually running right now, not just documented?"
        summary="Config Center now separates effective runtime config from derived policy blocks, so the operator can inspect exact values and then understand how they are interpreted."
        title="Config Center"
      />

      {queryError ? (
        <Banner
          detail={queryError}
          title="Config query failed"
          tone="danger"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface className="grid-span-8" eyebrow="Runtime summary" title="Effective posture">
          <div className="metric-ribbon">
            <div className="metric-ribbon__item">
              <span>Dry run</span>
              <strong>{String(effective?.DRY_RUN ? "yes" : "no")}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Trade amount</span>
              <strong>{formatDecimal(typeof effective?.TRADE_AMOUNT_SOL === "number" ? effective.TRADE_AMOUNT_SOL : null, " SOL")}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>AI threshold</span>
              <strong>{formatDecimal(typeof effective?.AI_THRESHOLD === "number" ? effective.AI_THRESHOLD : null)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>ML gate mode</span>
              <strong>{String(effective?.ML_GATE_MODE || "n/a")}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Max active positions</span>
              <strong>{formatCount(typeof effective?.MAX_ACTIVE_POSITIONS === "number" ? effective.MAX_ACTIVE_POSITIONS : null)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Dynamic sizing</span>
              <strong>{String(effective?.DYNAMIC_SIZING_ENABLED ? "on" : "off")}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Source truth" title="Config provenance">
          <SourceHealthStrip sources={sourceStatus} />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Filter policy" title="Pre-buy policy">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Profile by discovery</span>
              <strong>{String(policies?.filters.profile_by_discovery ? "on" : "off")}</strong>
            </div>
            <div className="kv-cell">
              <span>Snapshot filter</span>
              <strong>{String(policies?.filters.snapshot_quality_filter_enabled ? "on" : "off")}</strong>
            </div>
            <div className="kv-cell">
              <span>Max missing fields</span>
              <strong>{formatCount(typeof policies?.filters.snapshot_max_missing_fields === "number" ? policies.filters.snapshot_max_missing_fields : null)}</strong>
            </div>
            <div className="kv-cell">
              <span>Allowed price sources</span>
              <strong>{stringifyValue(policies?.filters.snapshot_allowed_price_sources)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Sizing policy" title="Capital allocation">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Dynamic sizing</span>
              <strong>{String(policies?.sizing.dynamic_sizing_enabled ? "on" : "off")}</strong>
            </div>
            <div className="kv-cell">
              <span>Pump early max age</span>
              <strong>{formatDecimal(typeof policies?.sizing.pump_early_max_age_min === "number" ? policies.sizing.pump_early_max_age_min : null, "m")}</strong>
            </div>
            <div className="kv-cell">
              <span>Size multipliers</span>
              <strong>{stringifyValue(policies?.sizing.size_multipliers)}</strong>
            </div>
            <div className="kv-cell">
              <span>Regime caps</span>
              <strong>{stringifyValue(policies?.sizing.regime_size_caps)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Exit policy" title="Close behavior">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Profile by regime</span>
              <strong>{String(policies?.exit.exit_profile_by_regime ? "on" : "off")}</strong>
            </div>
            <div className="kv-cell">
              <span>Partial enabled</span>
              <strong>{String(policies?.exit.tp_partial_enabled ? "on" : "off")}</strong>
            </div>
            <div className="kv-cell">
              <span>Partial trigger</span>
              <strong>{formatDecimal(typeof policies?.exit.tp_partial_trigger_pct === "number" ? policies.exit.tp_partial_trigger_pct : null, "%")}</strong>
            </div>
            <div className="kv-cell">
              <span>Time stop</span>
              <strong>{formatDecimal(typeof policies?.exit.time_stop_min === "number" ? policies.exit.time_stop_min : null, "m")}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-12" eyebrow="Strategy policy" title="Regime-specific execution rules" subtitle="Each card opens the full derived strategy payload.">
          <div className="strategy-grid">
            {strategyPolicies.map(({ regime, payload }) => (
              <button
                className="strategy-card strategy-card--interactive"
                key={regime}
                onClick={() => openRawRecord(regime, "Full derived strategy policy for this regime.", payload)}
                type="button"
              >
                <div className="strategy-card__header">
                  <strong>{regime}</strong>
                  <StatusChip label={String(payload?.mode || "n/a")} tone={payload?.mode === "live" ? "success" : "warn"} compact mono />
                </div>
                <div className="strategy-card__stats">
                  <div>
                    <span>Confirmations</span>
                    <strong>{formatCount(typeof payload?.confirmations === "number" ? payload.confirmations : null)}</strong>
                  </div>
                  <div>
                    <span>Backoff</span>
                    <strong>{formatDecimal(typeof payload?.backoff_s === "number" ? payload.backoff_s : null, "s")}</strong>
                  </div>
                  <div>
                    <span>Min age</span>
                    <strong>{formatDecimal(typeof payload?.min_age_min === "number" ? payload.min_age_min : null, "m")}</strong>
                  </div>
                  <div>
                    <span>Recovery cap</span>
                    <strong>{formatDecimal(typeof payload?.recovery_cap === "number" ? payload.recovery_cap : null)}</strong>
                  </div>
                </div>
              </button>
            ))}
          </div>
        </Surface>

        <Surface
          className="grid-span-12"
          eyebrow="Effective config"
          title="Exact runtime keys"
          subtitle="Search by key name to inspect the effective runtime config that the bot loaded."
          actions={
            <button className="ui-button ui-button--ghost" onClick={() => openRawRecord("Effective config", "Raw effective config payload.", effective)} type="button">
              Open raw config
            </button>
          }
        >
          <div className="filter-stack">
            <label className="filter-field">
              <span>Search key</span>
              <input
                className="ui-field"
                onChange={(event) => setQuery(event.target.value)}
                placeholder="AI_THRESHOLD, EXIT, PUMP_EARLY..."
                type="search"
                value={query}
              />
            </label>
          </div>
          <DataTable
            columns={configColumns}
            emptyMessage="No config keys match the current search."
            rowKey={(row) => row.key}
            rows={effectiveRows}
          />
        </Surface>
      </div>
    </div>
  );
}
