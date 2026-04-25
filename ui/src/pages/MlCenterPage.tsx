import { useDrawer } from "../app/drawer";
import { Banner } from "../components/primitives/Banner";
import { PageHero } from "../components/primitives/PageHero";
import { SourceHealthStrip } from "../components/primitives/SourceHealthStrip";
import { StatusChip } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type { MlResearchData, MlStatusData } from "../lib/api";
import { formatCount, formatDecimal, formatSignedPct, formatTimestamp, formatUsd, shortenPath } from "../lib/format";


function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
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


function boolLabel(value: boolean | null | undefined) {
  if (value === null || value === undefined) {
    return "n/a";
  }
  return value ? "yes" : "no";
}


function boolTone(value: boolean | null | undefined) {
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


export function MlCenterPage() {
  const { openPanel } = useDrawer();
  const statusQuery = usePollEnvelope<MlStatusData>("/api/v1/ml/status", 15000);
  const researchQuery = usePollEnvelope<MlResearchData>("/api/v1/ml/research", 15000);

  const status = statusQuery.envelope?.data;
  const research = researchQuery.envelope?.data;
  const sourceStatus = [
    ...(statusQuery.envelope?.meta.source_status || []),
    ...(researchQuery.envelope?.meta.source_status || []),
  ].filter((item, index, items) => items.findIndex((other) => other.source_key === item.source_key) === index);
  const queryError = statusQuery.error || researchQuery.error;

  const scorecard = asRecord(research?.scorecard);
  const thresholds = asRecord(research?.thresholds);
  const postPartialExperiment = asRecord(research?.post_partial_experiment);
  const consistency = research?.consistency;
  const thresholdRegimes = asRecord(thresholds?.regimes) || {};
  const thresholdEntries = Object.entries(thresholdRegimes).map(([regime, value]) => ({
    regime,
    payload: asRecord(value),
  }));
  const experimentCandidate = asRecord(postPartialExperiment?.candidate);
  const experimentForward = asRecord(postPartialExperiment?.forward_window);
  const experimentNotes = asRecord(postPartialExperiment?.notes);

  function openRawRecord(title: string, description: string, value: unknown) {
    openPanel({
      eyebrow: "ML / raw payload",
      title,
      description,
      content: (
        <div className="drawer-stack">
          <pre className="drawer-note">{JSON.stringify(value, null, 2)}</pre>
        </div>
      ),
    });
  }

  if (!status && !research && !queryError) {
    return (
      <Surface eyebrow="Inspect / ML" title="ML Center" subtitle="Waiting for the first ML payloads">
        <p>The page is polling `/api/v1/ml/status` and `/api/v1/ml/research`.</p>
      </Surface>
    );
  }

  return (
    <div className="page-stack">
      <PageHero
        eyebrow="Inspect / model"
        meta={
          <>
            <StatusChip
              label={statusQuery.envelope?.meta.degraded ? "ml degraded" : "ml ready"}
              tone={statusQuery.envelope?.meta.degraded ? "warn" : "success"}
            />
            <StatusChip label={status?.gate.mode || "unknown"} tone="info" compact mono />
            <StatusChip label={status?.runtime.model_loaded ? "model loaded" : "model missing"} tone={status?.runtime.model_loaded ? "success" : "warn"} compact />
            <StatusChip label={`research ${formatCount(research?.research_events.rows)} rows`} tone="neutral" compact />
          </>
        }
        question="Is the model usable, coherent, and safe to trust in runtime?"
        summary="ML Center now exposes runtime artifacts, gate posture, research scorecard freshness, and thresholds per regime without pretending missing files are healthy."
        title="ML Center"
      />

      {!status?.runtime.model_exists || !status?.runtime.meta_exists ? (
        <Banner
          detail="The model artifact or its metadata file is missing in this repo state. Runtime gate posture is still shown, but the model is not loadable."
          title="Model artifacts missing"
          tone="warn"
        />
      ) : null}

      {researchQuery.envelope?.meta.stale ? (
        <Banner
          detail="Research scorecard or thresholds are older than the latest research events. The page keeps rendering them, but flags the research side as stale."
          title="Research stale"
          tone="warn"
        />
      ) : null}

      {consistency && (!consistency.is_consistent || consistency.scorecard_stale_vs_latest_close) ? (
        <Banner
          detail={`DB=${formatCount(consistency.db_closed_rows)} | scorecard=${formatCount(consistency.scorecard_live_closed)} | lag=${formatCount(consistency.lag_rows)} | latest close ${formatTimestamp(consistency.latest_closed_at)}`}
          title="Research behind live ledger"
          tone="warn"
        />
      ) : null}

      {experimentForward && experimentForward.ready_for_review === true ? (
        <Banner
          detail={`Collected ${formatCount(numericValue(experimentForward.new_closed_trades))}/${formatCount(numericValue(experimentForward.gate_target_new_closes))} new paper closes with ${formatUsd(numericValue(experimentForward.delta_total_pnl_usd))} delta and drawdown guardrail passing.`}
          title="Post-partial experiment ready for review"
          tone="success"
        />
      ) : null}

      {queryError ? (
        <Banner
          detail={queryError}
          title="ML query failed"
          tone="danger"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface className="grid-span-8" eyebrow="Gate posture" title="Runtime gate and model state">
          <div className="metric-ribbon">
            <div className="metric-ribbon__item">
              <span>Gate mode</span>
              <strong>{status?.gate.mode || "n/a"}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Threshold</span>
              <strong>{formatDecimal(status?.gate.threshold)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Activation ready</span>
              <strong>{boolLabel(status?.gate.activation_ready)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Enforced</span>
              <strong>{boolLabel(status?.gate.enforced)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Source truth" title="ML provenance">
          <SourceHealthStrip sources={sourceStatus} />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Model runtime" title="Artifacts and dataset">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Model exists</span>
              <strong>{boolLabel(status?.runtime.model_exists)}</strong>
            </div>
            <div className="kv-cell">
              <span>Meta exists</span>
              <strong>{boolLabel(status?.runtime.meta_exists)}</strong>
            </div>
            <div className="kv-cell">
              <span>Model loaded</span>
              <strong>{boolLabel(status?.runtime.model_loaded)}</strong>
            </div>
            <div className="kv-cell">
              <span>Features count</span>
              <strong>{formatCount(status?.runtime.features_count)}</strong>
            </div>
            <div className="kv-cell">
              <span>Dataset rows</span>
              <strong>{formatCount(status?.runtime.rows)}</strong>
            </div>
            <div className="kv-cell">
              <span>Dataset quality passed</span>
              <strong>{boolLabel(status?.runtime.dataset_quality_passed)}</strong>
            </div>
            <div className="kv-cell">
              <span>Model path</span>
              <strong>{shortenPath(status?.runtime.model_path)}</strong>
            </div>
            <div className="kv-cell">
              <span>Meta path</span>
              <strong>{shortenPath(status?.runtime.meta_path)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Optional artifacts" title="Train and threshold files">
          <div className="strategy-grid">
            {[
              { label: "Train status", value: status?.train_status },
              { label: "Recommended threshold", value: status?.recommended_threshold },
              { label: "Dataset quality", value: status?.dataset_quality },
            ].map((item) => (
              <button
                className="strategy-card strategy-card--interactive"
                key={item.label}
                onClick={() => openRawRecord(item.label, "Raw artifact payload from disk.", item.value)}
                type="button"
              >
                <div className="strategy-card__header">
                  <strong>{item.label}</strong>
                  <StatusChip label={item.value ? "present" : "missing"} tone={item.value ? "success" : "warn"} compact />
                </div>
                <div className="strategy-card__footer">
                  <span>{item.value ? "Open raw payload" : "No artifact on disk"}</span>
                </div>
              </button>
            ))}
          </div>
        </Surface>

        <Surface
          className="grid-span-4"
          eyebrow="Paper-first experiment"
          title="Post-partial shadow cohort"
          actions={
            <div className="page-hero__actions">
              <button
                className="ui-button ui-button--ghost"
                onClick={() => openRawRecord("Post-partial experiment", "Raw paper-shadow experiment payload.", research?.post_partial_experiment)}
                type="button"
              >
                Open experiment
              </button>
            </div>
          }
        >
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Mode</span>
              <strong>{String(postPartialExperiment?.mode || "n/a")}</strong>
            </div>
            <div className="kv-cell">
              <span>Regime</span>
              <strong>{String(experimentCandidate?.entry_regime || "n/a")}</strong>
            </div>
            <div className="kv-cell">
              <span>Locked threshold</span>
              <strong>{formatDecimal(numericValue(experimentCandidate?.ml_threshold_locked))}</strong>
            </div>
            <div className="kv-cell">
              <span>Lock floor</span>
              <strong>{formatSignedPct(numericValue(experimentCandidate?.lock_floor_pct))}</strong>
            </div>
            <div className="kv-cell">
              <span>Giveback cap</span>
              <strong>{formatSignedPct(numericValue(experimentCandidate?.max_giveback_after_partial_pct))}</strong>
            </div>
            <div className="kv-cell">
              <span>New closes</span>
              <strong>
                {formatCount(numericValue(experimentForward?.new_closed_trades))}/
                {formatCount(numericValue(experimentForward?.gate_target_new_closes))}
              </strong>
            </div>
            <div className="kv-cell">
              <span>PnL delta</span>
              <strong>{formatUsd(numericValue(experimentForward?.delta_total_pnl_usd))}</strong>
            </div>
            <div className="kv-cell">
              <span>Guardrail</span>
              <strong>{boolLabel(experimentForward?.drawdown_guardrail_passed as boolean | null | undefined)}</strong>
            </div>
            <div className="kv-cell">
              <span>Ready for review</span>
              <strong>{boolLabel(experimentForward?.ready_for_review as boolean | null | undefined)}</strong>
            </div>
            <div className="kv-cell">
              <span>Execution changed</span>
              <strong>{boolLabel(experimentNotes?.paper_execution_changed as boolean | null | undefined)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Research scorecard" title="Shadow and scorecard posture">
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Decision rows</span>
              <strong>{formatCount(numericValue(scorecard?.decision_rows))}</strong>
            </div>
            <div className="kv-cell">
              <span>Outcome rows</span>
              <strong>{formatCount(numericValue(scorecard?.outcome_rows))}</strong>
            </div>
            <div className="kv-cell">
              <span>Live closed</span>
              <strong>{formatCount(numericValue(scorecard?.live_closed))}</strong>
            </div>
            <div className="kv-cell">
              <span>Research closed</span>
              <strong>{formatCount(numericValue(scorecard?.research_closed))}</strong>
            </div>
            <div className="kv-cell">
              <span>Live avg PnL</span>
              <strong>{formatSignedPct(numericValue(scorecard?.live_avg_pnl_pct))}</strong>
            </div>
            <div className="kv-cell">
              <span>Research avg PnL</span>
              <strong>{formatSignedPct(numericValue(scorecard?.research_avg_pnl_pct))}</strong>
            </div>
            <div className="kv-cell">
              <span>Profitable shadows</span>
              <strong>{formatCount(numericValue(scorecard?.profitable_research_shadows))}</strong>
            </div>
            <div className="kv-cell">
              <span>Research last event</span>
              <strong>{formatTimestamp(research?.research_events.last_event_at)}</strong>
            </div>
            <div className="kv-cell">
              <span>Ledger latest close</span>
              <strong>{formatTimestamp(consistency?.latest_closed_at)}</strong>
            </div>
            <div className="kv-cell">
              <span>Ledger lag rows</span>
              <strong>{formatCount(consistency?.lag_rows)}</strong>
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-12" eyebrow="Thresholds by regime" title="Research-derived thresholds" subtitle="Each card opens the full nested threshold payload in the drawer.">
          <div className="strategy-grid">
            {thresholdEntries.map(({ regime, payload }) => {
              const rankScore = asRecord(payload?.rank_score);
              return (
                <button
                  className="strategy-card strategy-card--interactive"
                  key={regime}
                  onClick={() => openRawRecord(regime, "Full threshold payload for this regime.", payload)}
                  type="button"
                >
                  <div className="strategy-card__header">
                    <strong>{regime}</strong>
                    <StatusChip
                      label={String(rankScore?.activation_ready ?? "n/a")}
                      tone={numericValue(rankScore?.activation_ready) ? "success" : "warn"}
                      compact
                    />
                  </div>
                  <div className="strategy-card__stats">
                    <div>
                      <span>Picked</span>
                      <strong>{formatDecimal(numericValue(rankScore?.picked))}</strong>
                    </div>
                    <div>
                      <span>Precision</span>
                      <strong>{formatDecimal(numericValue(rankScore?.precision_at_picked))}</strong>
                    </div>
                    <div>
                      <span>Selected rows</span>
                      <strong>{formatCount(numericValue(rankScore?.selected_rows_at_picked))}</strong>
                    </div>
                    <div>
                      <span>Avg realized PnL</span>
                      <strong>{formatSignedPct(numericValue(rankScore?.avg_realized_pnl_pct_at_picked))}</strong>
                    </div>
                  </div>
                  <div className="strategy-card__footer">
                    <span>{String(rankScore?.activation_reason || "no activation reason")}</span>
                  </div>
                </button>
              );
            })}
            {!thresholdEntries.length ? <p className="empty-note">No regime thresholds available.</p> : null}
          </div>
        </Surface>

        <Surface
          className="grid-span-12"
          eyebrow="Research artifacts"
          title="Raw scorecard and threshold payloads"
          actions={
            <div className="page-hero__actions">
              <button className="ui-button ui-button--ghost" onClick={() => openRawRecord("Research scorecard", "Raw scorecard JSON.", research?.scorecard)} type="button">
                Open scorecard
              </button>
              <button className="ui-button ui-button--ghost" onClick={() => openRawRecord("Research thresholds", "Raw thresholds JSON.", research?.thresholds)} type="button">
                Open thresholds
              </button>
            </div>
          }
        >
          <div className="kv-grid">
            <div className="kv-cell">
              <span>Scorecard generated</span>
              <strong>{formatTimestamp(typeof scorecard?.generated_at_utc === "string" ? scorecard.generated_at_utc : null)}</strong>
            </div>
            <div className="kv-cell">
              <span>Thresholds generated</span>
              <strong>{formatTimestamp(typeof thresholds?.generated_at_utc === "string" ? thresholds.generated_at_utc : null)}</strong>
            </div>
            <div className="kv-cell">
              <span>Threshold regimes</span>
              <strong>{formatCount(thresholdEntries.length)}</strong>
            </div>
            <div className="kv-cell">
              <span>Research events rows</span>
              <strong>{formatCount(research?.research_events.rows)}</strong>
            </div>
          </div>
        </Surface>
      </div>
    </div>
  );
}
