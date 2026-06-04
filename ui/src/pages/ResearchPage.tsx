import { useDrawer } from "../app/drawer";
import { Banner } from "../components/primitives/Banner";
import { DataTable, type DataColumn } from "../components/primitives/DataTable";
import { PageHero } from "../components/primitives/PageHero";
import { SourceHealthStrip } from "../components/primitives/SourceHealthStrip";
import { StatusChip } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type {
  ResearchApiBudgetData,
  ResearchCurrentBestData,
  ResearchMoonshotProgressData,
  ResearchPaperForwardData,
  ResearchPaperForwardItem,
  ResearchRunItem,
  ResearchRunsData,
  ResearchScoreboardData,
  ResearchScoreboardEntry,
  SourceStatus,
} from "../lib/api";
import { formatCount, formatDecimal, formatSignedPct, formatTimestamp, humanizeKey, shortenPath } from "../lib/format";


type ChipTone = "neutral" | "success" | "warn" | "danger" | "info";


function numericValue(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}


function asRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return {};
  }
  return value as Record<string, unknown>;
}


function statusTone(status: string | null | undefined): ChipTone {
  switch (String(status || "").toLowerCase()) {
    case "accepted_paper":
    case "accepted_replay":
    case "healthy":
    case "ok":
      return "success";
    case "needs_paper":
    case "paper_forward_started":
    case "inconclusive":
    case "warn":
      return "warn";
    case "rejected":
    case "rejected_paper":
    case "failed":
    case "blocked":
      return "danger";
    default:
      return "neutral";
  }
}


function safeText(value: unknown) {
  if (value === null || value === undefined || value === "") {
    return "n/a";
  }
  return String(value);
}


function statusRows(counts: Record<string, number> | undefined) {
  return Object.entries(counts || {})
    .map(([status, count]) => ({ status, count }))
    .sort((left, right) => right.count - left.count || left.status.localeCompare(right.status));
}


function uniqueSources(...sourceLists: Array<SourceStatus[] | undefined>): SourceStatus[] {
  return sourceLists
    .flatMap((items) => items || [])
    .filter((item, index, items) => items.findIndex((other) => other.source_key === item.source_key) === index);
}


const scoreboardColumns: DataColumn<ResearchScoreboardEntry>[] = [
  { id: "run", header: "Run", mono: true, render: (row) => row.run_id },
  { id: "proposal", header: "Proposal", mono: true, render: (row) => row.proposal_id },
  { id: "status", header: "Status", render: (row) => <StatusChip label={row.status} tone={statusTone(row.status)} compact mono /> },
  { id: "score", align: "right", header: "Score", render: (row) => formatDecimal(numericValue(row.objective_score)) },
  { id: "pnl", align: "right", header: "PnL d", render: (row) => formatSignedPct(numericValue(row.total_pnl_delta)) },
  { id: "median", align: "right", header: "Median d", render: (row) => formatSignedPct(numericValue(row.median_pnl_delta)) },
  { id: "runner", align: "right", header: "Runner d", render: (row) => formatDecimal(numericValue(row.runner_capture_delta)) },
  { id: "evaluated", header: "Evaluated", render: (row) => formatTimestamp(row.evaluated_at_utc) },
];


const runColumns: DataColumn<ResearchRunItem>[] = [
  { id: "run", header: "Run", mono: true, render: (row) => row.run_id },
  { id: "status", header: "Status", render: (row) => <StatusChip label={row.status} tone={statusTone(row.status)} compact mono /> },
  { id: "score", align: "right", header: "Score", render: (row) => formatDecimal(numericValue(row.objective_score)) },
  { id: "trades", align: "right", header: "Trades", render: (row) => formatCount(numericValue(row.replay_metrics.closed_trades)) },
  { id: "pnl", align: "right", header: "Replay PnL", render: (row) => formatDecimal(numericValue(row.replay_metrics.total_pnl_usd)) },
  { id: "updated", header: "Updated", render: (row) => formatTimestamp(row.updated_at) },
];


const paperColumns: DataColumn<ResearchPaperForwardItem>[] = [
  { id: "run", header: "Paper run", mono: true, render: (row) => row.run_id },
  { id: "status", header: "Status", render: (row) => <StatusChip label={row.status} tone={statusTone(row.status)} compact mono /> },
  { id: "profile", header: "Profile", mono: true, render: (row) => safeText(row.paper_profile) },
  { id: "score", align: "right", header: "Score", render: (row) => formatDecimal(numericValue(row.objective_score)) },
  { id: "started", header: "Started", render: (row) => formatTimestamp(row.started_at_utc) },
  { id: "finalized", header: "Finalized", render: (row) => formatTimestamp(row.finalized_at_utc) },
];


export function ResearchPage() {
  const { openPanel } = useDrawer();
  const scoreboardQuery = usePollEnvelope<ResearchScoreboardData>("/api/v1/research/scoreboard", 15000);
  const bestQuery = usePollEnvelope<ResearchCurrentBestData>("/api/v1/research/current-best", 15000);
  const apiBudgetQuery = usePollEnvelope<ResearchApiBudgetData>("/api/v1/research/api-budget", 30000);
  const moonshotQuery = usePollEnvelope<ResearchMoonshotProgressData>("/api/v1/research/moonshot-progress", 30000);
  const paperQuery = usePollEnvelope<ResearchPaperForwardData>("/api/v1/research/paper-forward", 15000);
  const runsQuery = usePollEnvelope<ResearchRunsData>("/api/v1/research/runs?limit=20", 15000);

  const scoreboard = scoreboardQuery.envelope?.data;
  const best = bestQuery.envelope?.data;
  const apiBudget = apiBudgetQuery.envelope?.data;
  const moonshot = moonshotQuery.envelope?.data;
  const paper = paperQuery.envelope?.data;
  const runs = runsQuery.envelope?.data;
  const queryError =
    scoreboardQuery.error ||
    bestQuery.error ||
    apiBudgetQuery.error ||
    moonshotQuery.error ||
    paperQuery.error ||
    runsQuery.error;

  const sources = uniqueSources(
    scoreboardQuery.envelope?.meta.source_status,
    bestQuery.envelope?.meta.source_status,
    apiBudgetQuery.envelope?.meta.source_status,
    moonshotQuery.envelope?.meta.source_status,
    paperQuery.envelope?.meta.source_status,
    runsQuery.envelope?.meta.source_status,
  );
  const currentBestPolicy = asRecord(best?.candidate_policy);
  const currentBestChanges = asRecord(currentBestPolicy.changes);
  const latestPaper = paper?.latest;
  const objectiveScore = numericValue(scoreboard?.summary.latest_objective_score) ?? numericValue(best?.objective_score);
  const bestObjectiveScore = numericValue(scoreboard?.summary.best_objective_score);
  const api429Count = numericValue(apiBudget?.summary.api_429_count);
  const degradedMinutes = numericValue(apiBudget?.summary.provider_degraded_minutes);
  const moonshotSummary = moonshot?.summary;
  const statusBreakdown = statusRows(scoreboard?.summary.status_counts);
  const paperBreakdown = statusRows(paper?.status_counts);

  function openRaw(title: string, description: string, payload: unknown) {
    openPanel({
      eyebrow: "AutoResearch / raw payload",
      title,
      description,
      content: (
        <div className="drawer-stack">
          <pre className="drawer-note">{JSON.stringify(payload, null, 2)}</pre>
        </div>
      ),
    });
  }

  if (!scoreboard && !queryError) {
    return (
      <Surface eyebrow="Inspect / AutoResearch" title="AutoResearch" subtitle="Waiting for local research artifacts">
        <p>Polling `/api/v1/research/scoreboard`.</p>
      </Surface>
    );
  }

  return (
    <div className="page-stack">
      <PageHero
        eyebrow="Inspect / AutoResearch"
        meta={
          <>
            <StatusChip label={safeText(scoreboard?.summary.latest_status || "no runs")} tone={statusTone(scoreboard?.summary.latest_status)} compact mono />
            <StatusChip label={`score ${formatDecimal(objectiveScore)}`} tone={objectiveScore !== null && objectiveScore > 0 ? "success" : "warn"} compact />
            <StatusChip label={`API 429 ${formatCount(api429Count)}`} tone={api429Count ? "danger" : "success"} compact />
            <StatusChip label={`paper ${safeText(latestPaper?.status || "none")}`} tone={statusTone(latestPaper?.status)} compact mono />
          </>
        }
        question="Which candidate is currently strongest, and where is the research loop blocked?"
        summary="Scoreboard, current best policy, replay runs, paper-forward status, API budget and moonshot progress from local AutoResearch artifacts."
        title="AutoResearch panel"
      />

      {queryError ? <Banner detail={queryError} title="Research query failed" tone="danger" /> : null}
      {api429Count || degradedMinutes ? (
        <Banner
          detail={`api_429_count=${formatCount(api429Count)} provider_degraded_minutes=${formatCount(degradedMinutes)}`}
          title="API budget gate is warning"
          tone="warn"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface className="grid-span-4" eyebrow="Current best" title={safeText(best?.proposal_id || "No accepted candidate")}>
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Status</span>
              <strong>{safeText(best?.status)}</strong>
            </div>
            <div className="kv-cell">
              <span>Score</span>
              <strong>{formatDecimal(numericValue(best?.objective_score))}</strong>
            </div>
            <div className="kv-cell">
              <span>Run</span>
              <strong>{safeText(best?.run_id)}</strong>
            </div>
            <div className="kv-cell">
              <span>Source</span>
              <strong>{safeText(best?.source)}</strong>
            </div>
          </div>
          <div className="strategy-grid">
            {Object.entries(currentBestChanges).slice(0, 6).map(([key, value]) => (
              <div className="strategy-card" key={key}>
                <div className="strategy-card__header">
                  <strong>{key}</strong>
                  <StatusChip label={safeText(value)} tone="info" compact mono />
                </div>
              </div>
            ))}
            {!Object.keys(currentBestChanges).length ? <p className="empty-note">No candidate changes available.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Objective" title="Score posture">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Latest score</span>
              <strong>{formatDecimal(objectiveScore)}</strong>
            </div>
            <div className="kv-cell">
              <span>Best score</span>
              <strong>{formatDecimal(bestObjectiveScore)}</strong>
            </div>
            <div className="kv-cell">
              <span>Accepted</span>
              <strong>{formatCount(numericValue(scoreboard?.summary.accepted_count))}</strong>
            </div>
            <div className="kv-cell">
              <span>Rejected</span>
              <strong>{formatCount(numericValue(scoreboard?.summary.rejected_count))}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Source truth" title="Research artifact health">
          <SourceHealthStrip sources={sources} />
        </Surface>

        <Surface className="grid-span-8" eyebrow="Scoreboard" title="Candidate keep/discard ledger">
          <DataTable
            columns={scoreboardColumns}
            emptyMessage="No AutoResearch scoreboard entries available."
            rowKey={(row) => `${row.run_id}-${row.proposal_id}`}
            rows={scoreboard?.entries || []}
          />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Status mix" title="Run outcomes">
          <div className="breakdown-list">
            {statusBreakdown.map((row) => (
              <div className="breakdown-list__item" key={row.status}>
                <div className="breakdown-list__label">
                  <strong>{row.status}</strong>
                  <span>{formatCount(row.count)}</span>
                </div>
              </div>
            ))}
            {!statusBreakdown.length ? <p className="empty-note">No status rows available.</p> : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="API budget" title={safeText(apiBudget?.summary.status || "unknown")}>
          <div className="kv-grid">
            <div className="kv-cell">
              <span>API 429</span>
              <strong>{formatCount(api429Count)}</strong>
            </div>
            <div className="kv-cell">
              <span>Provider degraded</span>
              <strong>{formatCount(degradedMinutes)}</strong>
            </div>
            <div className="kv-cell">
              <span>Cooldowns</span>
              <strong>{formatCount(numericValue(apiBudget?.summary.cooldown_count))}</strong>
            </div>
            <div className="kv-cell">
              <span>RPC errors</span>
              <strong>{formatCount(numericValue(apiBudget?.summary.rpc_errors))}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Moonshot progress" title="Tail capture">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Candidates seen</span>
              <strong>{formatCount(numericValue(moonshotSummary?.moonshot_candidates_seen))}</strong>
            </div>
            <div className="kv-cell">
              <span>Paper buys</span>
              <strong>{formatCount(numericValue(moonshotSummary?.moonshot_buys))}</strong>
            </div>
            <div className="kv-cell">
              <span>Peak 100 capture</span>
              <strong>{formatDecimal(numericValue(moonshotSummary?.moonshot_peak100_capture))}</strong>
            </div>
            <div className="kv-cell">
              <span>Runner capture</span>
              <strong>{formatDecimal(numericValue(moonshotSummary?.runner_capture_ratio))}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Paper forward" title={safeText(latestPaper?.run_id || "No paper run")}>
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Status</span>
              <strong>{safeText(latestPaper?.status)}</strong>
            </div>
            <div className="kv-cell">
              <span>Score</span>
              <strong>{formatDecimal(numericValue(latestPaper?.objective_score))}</strong>
            </div>
            <div className="kv-cell">
              <span>Active</span>
              <strong>{formatCount(numericValue(paper?.active.length))}</strong>
            </div>
            <div className="kv-cell">
              <span>Demotion</span>
              <strong>{safeText(asRecord(paper?.demotion_latest).status)}</strong>
            </div>
          </div>
          <div className="breakdown-list">
            {paperBreakdown.map((row) => (
              <div className="breakdown-list__item" key={row.status}>
                <div className="breakdown-list__label">
                  <strong>{row.status}</strong>
                  <span>{formatCount(row.count)}</span>
                </div>
              </div>
            ))}
          </div>
        </Surface>

        <Surface className="grid-span-8" eyebrow="Replay runs" title="Local replay snapshots">
          <DataTable
            columns={runColumns}
            emptyMessage="No replay run snapshots available."
            rowKey={(row) => row.run_id}
            rows={runs?.items || []}
          />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Missed moonshots" title="Uncaptured tail">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Missed peak 100</span>
              <strong>{formatCount(numericValue(moonshotSummary?.missed_peak100_count))}</strong>
            </div>
            <div className="kv-cell">
              <span>Missed peak 500</span>
              <strong>{formatCount(numericValue(moonshotSummary?.missed_peak500_count))}</strong>
            </div>
            <div className="kv-cell">
              <span>Missed peak 1000</span>
              <strong>{formatCount(numericValue(moonshotSummary?.missed_peak1000_count))}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-8" eyebrow="Paper-forward runs" title="Paper validation history">
          <DataTable
            columns={paperColumns}
            emptyMessage="No paper-forward runs available."
            rowKey={(row) => row.run_id}
            rows={paper?.items || []}
          />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Artifacts" title="Raw payloads">
          <div className="strategy-grid">
            {[
              { label: "Best policy", value: best?.proposal_path ? shortenPath(best.proposal_path) : best?.source, payload: best },
              { label: "API budget", value: apiBudget?.summary.status, payload: apiBudget },
              { label: "Moonshot report", value: moonshotSummary?.moonshot_candidates_seen, payload: moonshot },
              { label: "Paper latest", value: latestPaper?.status, payload: latestPaper },
            ].map((item) => (
              <button
                className="strategy-card strategy-card--interactive"
                key={item.label}
                onClick={() => openRaw(item.label, "Local AutoResearch artifact payload.", item.payload)}
                type="button"
              >
                <div className="strategy-card__header">
                  <strong>{item.label}</strong>
                  <StatusChip label={item.payload ? "present" : "missing"} tone={item.payload ? "success" : "warn"} compact />
                </div>
                <div className="strategy-card__footer">
                  <span>{safeText(item.value)}</span>
                </div>
              </button>
            ))}
          </div>
        </Surface>
      </div>
    </div>
  );
}
