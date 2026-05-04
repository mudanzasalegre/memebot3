import { useDrawer } from "../app/drawer";
import { Banner } from "../components/primitives/Banner";
import { DataTable, type DataColumn } from "../components/primitives/DataTable";
import { PageHero } from "../components/primitives/PageHero";
import { SourceHealthStrip } from "../components/primitives/SourceHealthStrip";
import { StatusChip } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type {
  PolicyDecisionLedgerData,
  PolicyFunnelData,
  PolicyFunnelItem,
  PolicyGateItem,
  PolicyMetricRow,
  PolicyModelRegistryData,
  PolicyProposalItem,
  PolicyProposalsData,
  PolicyReplayData,
  PolicyRunnerCaptureData,
  PolicySafetyData,
  PolicyTradeDiagnosticsData,
} from "../lib/api";
import { formatCount, formatDecimal, formatSignedPct, formatTimestamp, humanizeKey, shortenPath } from "../lib/format";


type ChipTone = "neutral" | "success" | "warn" | "danger" | "info";


function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}


function numericValue(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
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


function gateTone(status: string | null | undefined): ChipTone {
  switch (status) {
    case "pass":
      return "success";
    case "warn":
      return "warn";
    case "block":
    case "missing":
      return "danger";
    default:
      return "neutral";
  }
}


function actionTone(action: unknown): ChipTone {
  switch (String(action || "").toLowerCase()) {
    case "buy":
      return "success";
    case "shadow":
    case "delay":
      return "warn";
    case "execution_blocked":
      return "danger";
    case "reject":
      return "neutral";
    default:
      return "info";
  }
}


function countRows(value: Record<string, number> | undefined) {
  return Object.entries(value || {})
    .map(([key, count]) => ({ key, count }))
    .sort((left, right) => right.count - left.count || left.key.localeCompare(right.key));
}


function topDiagnosticGroups(groups: Record<string, Record<string, unknown>> | undefined) {
  return Object.entries(groups || {})
    .map(([group, payload]) => ({ group, ...payload }) as Record<string, unknown> & { group: string })
    .sort((left, right) => (numericValue(right.total_pnl_points) ?? 0) - (numericValue(left.total_pnl_points) ?? 0))
    .slice(0, 12);
}


const gateColumns: DataColumn<PolicyGateItem>[] = [
  {
    id: "label",
    header: "Gate",
    render: (row) => row.label,
  },
  {
    id: "status",
    header: "Status",
    render: (row) => <StatusChip label={row.status} tone={gateTone(row.status)} compact mono />,
  },
  {
    id: "detail",
    header: "Detail",
    render: (row) => row.detail || "n/a",
  },
];


const replayColumns: DataColumn<PolicyMetricRow>[] = [
  {
    id: "policy",
    header: "Policy",
    mono: true,
    render: (row) => row.policy,
  },
  {
    id: "trades",
    align: "right",
    header: "Trades",
    render: (row) => formatCount(numericValue(row.trades)),
  },
  {
    id: "win",
    align: "right",
    header: "Win",
    render: (row) => formatDecimal(numericValue(row.win_rate), "%"),
  },
  {
    id: "avg",
    align: "right",
    header: "Avg PnL",
    render: (row) => formatSignedPct(numericValue(row.avg_pnl)),
  },
  {
    id: "total",
    align: "right",
    header: "Total",
    render: (row) => formatSignedPct(numericValue(row.total_pnl)),
  },
  {
    id: "severe",
    align: "right",
    header: "Severe",
    render: (row) => formatCount(numericValue(row.severe_loss_count)),
  },
  {
    id: "runner",
    align: "right",
    header: "Runner capture",
    render: (row) => formatDecimal(numericValue(row.runner_capture_ratio)),
  },
];


const funnelColumns: DataColumn<PolicyFunnelItem>[] = [
  {
    id: "address",
    header: "Address",
    mono: true,
    render: (row) => row.address,
  },
  {
    id: "state",
    header: "Final state",
    render: (row) => <StatusChip label={row.final_state} tone={actionTone(row.final_state)} compact mono />,
  },
  {
    id: "stage",
    header: "Primary stage",
    mono: true,
    render: (row) => row.primary_stage,
  },
  {
    id: "reason",
    header: "Blocking reason",
    mono: true,
    render: (row) => row.final_blocking_reason,
  },
  {
    id: "peak",
    align: "right",
    header: "Confirmed peak",
    render: (row) => formatSignedPct(numericValue(row.confirmed_later_peak_pct)),
  },
];


const ledgerColumns: DataColumn<Record<string, unknown>>[] = [
  {
    id: "time",
    header: "Time",
    render: (row) => formatTimestamp(typeof row.timestamp === "string" ? row.timestamp : null),
  },
  {
    id: "address",
    header: "Address",
    mono: true,
    render: (row) => String(row.address || "n/a"),
  },
  {
    id: "lane",
    header: "Lane",
    mono: true,
    render: (row) => String(row.lane || "unknown"),
  },
  {
    id: "decision",
    header: "Action",
    render: (row) => <StatusChip label={String(row.decision || row.action || "unknown")} tone={actionTone(row.decision || row.action)} compact mono />,
  },
  {
    id: "reason",
    header: "Reason",
    mono: true,
    render: (row) => String(row.reason || "n/a"),
  },
  {
    id: "score",
    align: "right",
    header: "Policy score",
    render: (row) => {
      const scores = asRecord(row.scores);
      return formatDecimal(numericValue(row.policy_score) ?? numericValue(scores?.policy_score));
    },
  },
];


const proposalColumns: DataColumn<PolicyProposalItem>[] = [
  {
    id: "proposal",
    header: "Proposal",
    mono: true,
    render: (row) => row.proposal_id,
  },
  {
    id: "status",
    header: "Status",
    render: (row) => <StatusChip label={row.status} tone={row.folder === "accepted" ? "success" : row.folder === "rejected" ? "danger" : "info"} compact />,
  },
  {
    id: "score",
    align: "right",
    header: "Score",
    render: (row) => formatDecimal(numericValue(row.expected_metrics?.score)),
  },
  {
    id: "live",
    header: "Live",
    render: (row) => <StatusChip label={boolLabel(row.live_allowed)} tone={row.live_allowed ? "danger" : "success"} compact />,
  },
  {
    id: "gates",
    header: "Required gates",
    render: (row) => row.required_gates?.join(", ") || "n/a",
  },
];


const diagnosticColumns: DataColumn<Record<string, unknown> & { group: string }>[] = [
  { id: "group", header: "Group", mono: true, render: (row) => String(row.group) },
  { id: "trades", align: "right", header: "Trades", render: (row) => formatCount(numericValue(row.trades)) },
  { id: "win", align: "right", header: "Win", render: (row) => formatDecimal(numericValue(row.win_rate), "%") },
  { id: "avg", align: "right", header: "Avg PnL", render: (row) => formatSignedPct(numericValue(row.avg_pnl)) },
  { id: "severe", align: "right", header: "Severe", render: (row) => formatCount(numericValue(row.severe_loss_count)) },
];


export function PolicyCenterPage() {
  const { openPanel } = useDrawer();
  const safetyQuery = usePollEnvelope<PolicySafetyData>("/api/v1/policy/safety", 15000);
  const replayQuery = usePollEnvelope<PolicyReplayData>("/api/v1/policy/replay", 60000);
  const funnelQuery = usePollEnvelope<PolicyFunnelData>("/api/v1/policy/funnel-attribution?limit=25", 30000);
  const ledgerQuery = usePollEnvelope<PolicyDecisionLedgerData>("/api/v1/policy/decision-ledger?limit=25", 15000);
  const diagnosticsQuery = usePollEnvelope<PolicyTradeDiagnosticsData>("/api/v1/policy/trade-diagnostics", 60000);
  const runnerQuery = usePollEnvelope<PolicyRunnerCaptureData>("/api/v1/policy/runner-capture", 60000);
  const proposalsQuery = usePollEnvelope<PolicyProposalsData>("/api/v1/policy/proposals?limit=12", 60000);
  const registryQuery = usePollEnvelope<PolicyModelRegistryData>("/api/v1/policy/model-registry", 60000);

  const safety = safetyQuery.envelope?.data;
  const replay = replayQuery.envelope?.data;
  const funnel = funnelQuery.envelope?.data;
  const ledger = ledgerQuery.envelope?.data;
  const diagnostics = diagnosticsQuery.envelope?.data;
  const runner = runnerQuery.envelope?.data;
  const proposals = proposalsQuery.envelope?.data;
  const registry = registryQuery.envelope?.data;
  const queryError =
    safetyQuery.error ||
    replayQuery.error ||
    funnelQuery.error ||
    ledgerQuery.error ||
    diagnosticsQuery.error ||
    runnerQuery.error ||
    proposalsQuery.error ||
    registryQuery.error;

  const sources = [
    ...(safetyQuery.envelope?.meta.source_status || []),
    ...(replayQuery.envelope?.meta.source_status || []),
    ...(funnelQuery.envelope?.meta.source_status || []),
    ...(ledgerQuery.envelope?.meta.source_status || []),
    ...(proposalsQuery.envelope?.meta.source_status || []),
    ...(registryQuery.envelope?.meta.source_status || []),
  ].filter((item, index, items) => items.findIndex((other) => other.source_key === item.source_key) === index);

  const gateRows = safety?.gates || [];
  const blockCount = gateRows.filter((row) => row.status === "block" || row.status === "missing").length;
  const warnCount = gateRows.filter((row) => row.status === "warn").length;
  const currentReplay = asRecord(safety?.policy_replay.current);
  const candidateReplay = asRecord(safety?.policy_replay.candidate);
  const configAuditRows = countRows(safety?.config_effect_summary);
  const ledgerActionRows = countRows(ledger?.summary.by_action);
  const ledgerLaneRows = countRows(ledger?.summary.by_lane);
  const diagnosticRows = topDiagnosticGroups(diagnostics?.groups);
  const runnerBuckets = Object.entries(runner?.summary || {}).map(([bucket, payload]) => ({ bucket, ...payload })) as Array<
    Record<string, unknown> & { bucket: string }
  >;
  const modelFamilies = registry?.families || [];

  function openRawRecord(title: string, description: string, value: unknown) {
    openPanel({
      eyebrow: "Policy / raw payload",
      title,
      description,
      content: (
        <div className="drawer-stack">
          <pre className="drawer-note">{JSON.stringify(value, null, 2)}</pre>
        </div>
      ),
    });
  }

  if (!safety && !queryError) {
    return (
      <Surface eyebrow="Inspect / learned policy" title="Policy Center" subtitle="Waiting for policy artifacts">
        <p>The page is polling `/api/v1/policy/safety` and derived learned-policy reports.</p>
      </Surface>
    );
  }

  return (
    <div className="page-stack">
      <PageHero
        eyebrow="Inspect / learned policy"
        meta={
          <>
            <StatusChip label={blockCount ? `${formatCount(blockCount)} blocking gates` : "live gates closed"} tone={blockCount ? "danger" : "success"} compact />
            <StatusChip label={`${formatCount(warnCount)} warnings`} tone={warnCount ? "warn" : "neutral"} compact />
            <StatusChip label={`best ${safety?.policy_replay.best_by_total_pnl || replay?.best_by_total_pnl || "n/a"}`} tone="info" compact mono />
            <StatusChip label={`proposals ${formatCount(proposals?.count)}`} tone="neutral" compact />
          </>
        }
        question="Can the learned-policy stack be trusted, and what proof exists before paper or live canary?"
        summary="Policy Center is the read-only control plane for PR-00..PR-36: safety gates, replay, funnel attribution, decision ledger, diagnostics, runner capture, model families and strategy proposals."
        title="Learned policy control plane"
      />

      {blockCount ? (
        <Banner
          detail="At least one gate is blocking live canary. This page is informational only; no model promotion, config change, or live activation is performed from here."
          title="Live canary is not cleared"
          tone="danger"
        />
      ) : null}

      {queryError ? <Banner detail={queryError} title="Policy query failed" tone="danger" /> : null}

      <div className="editorial-grid">
        <Surface className="grid-span-8" eyebrow="Safety" title="Rollout gate posture">
          <DataTable
            columns={gateColumns}
            emptyMessage="No safety gates reported."
            rowKey={(row) => row.id}
            rows={gateRows}
          />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Source truth" title="Policy provenance">
          <SourceHealthStrip sources={sources} />
        </Surface>

        <Surface className="grid-span-8" eyebrow="Replay judge" title="Policy replay comparison">
          <DataTable
            columns={replayColumns}
            emptyMessage="No policy replay rows available."
            rowKey={(row) => row.policy}
            rows={replay?.policies || []}
          />
        </Surface>

        <Surface
          className="grid-span-4"
          eyebrow="Candidate vs current"
          title="Replay gate"
          actions={<button className="ui-button ui-button--ghost" onClick={() => openRawRecord("Policy safety", "Full safety payload.", safety)} type="button">Open safety</button>}
        >
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Candidate passed</span>
              <strong>{boolLabel(safety?.policy_replay.candidate_passed)}</strong>
            </div>
            <div className="kv-cell">
              <span>Current total</span>
              <strong>{formatSignedPct(numericValue(currentReplay?.total_pnl))}</strong>
            </div>
            <div className="kv-cell">
              <span>Candidate total</span>
              <strong>{formatSignedPct(numericValue(candidateReplay?.total_pnl))}</strong>
            </div>
            <div className="kv-cell">
              <span>Current severe</span>
              <strong>{formatCount(numericValue(currentReplay?.severe_loss_count))}</strong>
            </div>
            <div className="kv-cell">
              <span>Candidate severe</span>
              <strong>{formatCount(numericValue(candidateReplay?.severe_loss_count))}</strong>
            </div>
            <div className="kv-cell">
              <span>Runner capture</span>
              <strong>{formatDecimal(numericValue(candidateReplay?.runner_capture_ratio))}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Invariants" title="Non-negotiable safety defaults">
          <div className="kv-grid">
            {Object.entries(safety?.invariants || {}).map(([key, value]) => (
              <div className="kv-cell" key={key}>
                <span>{humanizeKey(key)}</span>
                <strong>{boolLabel(value)}</strong>
              </div>
            ))}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Preflight and drift" title="Environment health">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Preflight ok</span>
              <strong>{boolLabel(safety?.preflight.ok)}</strong>
            </div>
            <div className="kv-cell">
              <span>Preflight generated</span>
              <strong>{formatTimestamp(typeof safety?.preflight.generated_at_utc === "string" ? safety.preflight.generated_at_utc : null)}</strong>
            </div>
            <div className="kv-cell">
              <span>Drift degraded</span>
              <strong>{boolLabel(safety?.drift.degraded)}</strong>
            </div>
            <div className="kv-cell">
              <span>Drift reason</span>
              <strong>{String(safety?.drift.reason || "n/a")}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Config audit" title="Flag effect coverage">
          <div className="strategy-grid">
            {configAuditRows.map((row) => (
              <div className="strategy-card" key={row.key}>
                <div className="strategy-card__header">
                  <strong>{humanizeKey(row.key)}</strong>
                  <StatusChip label={formatCount(row.count)} tone={row.key.includes("placebo") ? "warn" : "success"} compact />
                </div>
              </div>
            ))}
            {!configAuditRows.length ? <p className="empty-note">No config-effect audit summary available.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-6" eyebrow="Decision ledger" title="Recent decisions">
          <DataTable
            columns={ledgerColumns}
            emptyMessage="No decision ledger rows available."
            rowKey={(row) => String(row.decision_id || `${row.timestamp}-${row.address}-${row.reason}`)}
            rows={ledger?.items || []}
          />
        </Surface>

        <Surface className="grid-span-3" eyebrow="Ledger actions" title="Action mix">
          <div className="breakdown-list">
            {ledgerActionRows.map((row) => (
              <div className="breakdown-list__item" key={row.key}>
                <div className="breakdown-list__label">
                  <strong>{row.key}</strong>
                  <span>{formatCount(row.count)}</span>
                </div>
              </div>
            ))}
            {!ledgerActionRows.length ? <p className="empty-note">No action counts available.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-3" eyebrow="Ledger lanes" title="Lane mix">
          <div className="breakdown-list">
            {ledgerLaneRows.slice(0, 10).map((row) => (
              <div className="breakdown-list__item" key={row.key}>
                <div className="breakdown-list__label">
                  <strong>{row.key}</strong>
                  <span>{formatCount(row.count)}</span>
                </div>
              </div>
            ))}
            {!ledgerLaneRows.length ? <p className="empty-note">No lane counts available.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-8" eyebrow="Funnel attribution" title="Final state by token">
          <DataTable
            columns={funnelColumns}
            emptyMessage="No funnel attribution rows available."
            rowKey={(row) => row.address}
            rows={funnel?.items || []}
          />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Funnel blockers" title="Top primary blockers">
          <div className="breakdown-list">
            {(funnel?.summary.blocking_reasons || []).slice(0, 12).map((row) => (
              <div className="breakdown-list__item" key={row.key}>
                <div className="breakdown-list__label">
                  <strong>{row.key}</strong>
                  <span>{formatCount(row.count)}</span>
                </div>
              </div>
            ))}
            {!funnel?.summary.blocking_reasons?.length ? <p className="empty-note">No blockers available.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Runner capture" title="Capture by runner bucket">
          <div className="kv-grid">
            {runnerBuckets.map((row) => (
              <div className="kv-cell" key={row.bucket}>
                <span>{humanizeKey(row.bucket)}</span>
                <strong>{formatCount(numericValue(row.count))}</strong>
                <small>{`capture ${formatDecimal(numericValue(row.avg_capture_ratio))} / giveback ${formatSignedPct(numericValue(row.avg_giveback_pct))}`}</small>
              </div>
            ))}
            {!runnerBuckets.length ? <p className="empty-note">No runner buckets available.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-8" eyebrow="Trade diagnostics" title="Best and worst grouped outcomes">
          <DataTable
            columns={diagnosticColumns}
            emptyMessage="No diagnostic groups available."
            rowKey={(row) => String(row.group)}
            rows={diagnosticRows}
          />
        </Surface>

        <Surface className="grid-span-6" eyebrow="Model families" title="Registry by family">
          <div className="strategy-grid">
            {modelFamilies.map((family) => (
              <button
                className="strategy-card strategy-card--interactive"
                key={family.family}
                onClick={() => openRawRecord(`${family.family} registry`, "Raw registry payload for this model family.", family)}
                type="button"
              >
                <div className="strategy-card__header">
                  <strong>{family.family}</strong>
                  <StatusChip label={family.active_model_exists ? "active" : "manual only"} tone={family.active_model_exists ? "success" : "warn"} compact />
                </div>
                <div className="strategy-card__stats">
                  <div>
                    <span>Candidates</span>
                    <strong>{formatCount(family.candidate_count)}</strong>
                  </div>
                  <div>
                    <span>Meta</span>
                    <strong>{boolLabel(family.active_meta_exists)}</strong>
                  </div>
                </div>
                <div className="strategy-card__footer">
                  <span>{String(family.registry?.active_model_id || "no active family model")}</span>
                </div>
              </button>
            ))}
            {!modelFamilies.length ? <p className="empty-note">No model family registry rows available.</p> : null}
          </div>
        </Surface>

        <Surface
          className="grid-span-6"
          eyebrow="Strategy proposals"
          title="Candidate profiles"
          actions={<button className="ui-button ui-button--ghost" onClick={() => openRawRecord("Strategy proposals", "Raw proposal list.", proposals)} type="button">Open proposals</button>}
        >
          <DataTable
            columns={proposalColumns}
            emptyMessage="No candidate policy proposals available."
            rowKey={(row) => `${row.folder}-${row.proposal_id}`}
            rows={proposals?.items || []}
          />
        </Surface>

        <Surface className="grid-span-12" eyebrow="Artifacts" title="Operational artifact shortcuts">
          <div className="strategy-grid">
            {[
              { label: "Preflight", value: safety?.preflight.interpreter, payload: safety?.preflight },
              { label: "Paper forward", value: safety?.paper_forward.policy_name, payload: safety?.paper_forward },
              { label: "Policy replay raw", value: replay?.best_by_total_pnl, payload: replay?.raw },
              { label: "Model registry", value: shortenPath(typeof registryQuery.envelope?.meta.source_status?.[0]?.path === "string" ? registryQuery.envelope.meta.source_status[0].path : null), payload: registry },
            ].map((item) => (
              <button
                className="strategy-card strategy-card--interactive"
                key={item.label}
                onClick={() => openRawRecord(item.label, "Raw artifact payload.", item.payload)}
                type="button"
              >
                <div className="strategy-card__header">
                  <strong>{item.label}</strong>
                  <StatusChip label={item.payload ? "present" : "missing"} tone={item.payload ? "success" : "warn"} compact />
                </div>
                <div className="strategy-card__footer">
                  <span>{String(item.value || "open raw payload")}</span>
                </div>
              </button>
            ))}
          </div>
        </Surface>
      </div>
    </div>
  );
}
