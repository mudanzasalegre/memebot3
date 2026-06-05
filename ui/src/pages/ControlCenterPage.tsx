import { useEffect, useState } from "react";
import { useOutletContext, useSearchParams } from "react-router-dom";

import { useDrawer } from "../app/drawer";
import { useAuth } from "../auth/AuthProvider";
import type { ShellOutletContext } from "../components/layout/AppShell";
import { Banner } from "../components/primitives/Banner";
import { DataTable, type DataColumn } from "../components/primitives/DataTable";
import { PageHero } from "../components/primitives/PageHero";
import { SavedViewsToolbar } from "../components/primitives/SavedViewsToolbar";
import { SourceHealthStrip } from "../components/primitives/SourceHealthStrip";
import { StatusChip } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import {
  postEnvelope,
  type BotProcessData,
  type BotProcessStartRequest,
  type BotProcessStopRequest,
  type ControlCommandCreateData,
  type ControlCommandCreateRequest,
  type ControlCommandItem,
  type ControlCommandStatus,
  type ControlCommandType,
  type ControlCommandsData,
  type ControlStateData,
  type LivePromotionPreflightData,
  type SourceStatus,
} from "../lib/api";
import { formatCount, formatRelative, formatTimestamp, humanizeKey } from "../lib/format";


const statusOptions: Array<ControlCommandStatus | "all"> = ["all", "pending", "running", "done", "failed", "rejected", "cancelled"];
const reportOptions = ["baseline", "edge", "research"] as const;
const logLevelOptions = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;
const commandOrder: ControlCommandType[] = [
  "pause_discovery",
  "resume_discovery",
  "pause_buys",
  "resume_buys",
  "reload_model",
  "trigger_retrain",
  "refresh_reports",
  "set_log_level",
];

const commandCatalog: Array<{
  type: ControlCommandType;
  label: string;
  summary: string;
  confirmation: string;
}> = [
  {
    type: "pause_discovery",
    label: "Pause discovery",
    summary: "Stops new DexScreener and Pump.fun intake while keeping the rest of the runtime alive.",
    confirmation: "This halts new candidate intake. Monitoring, queue snapshots, and open positions continue.",
  },
  {
    type: "resume_discovery",
    label: "Resume discovery",
    summary: "Re-enables DexScreener and Pump.fun intake for new candidates.",
    confirmation: "This re-opens the intake side of the bot and can start filling the queue again immediately.",
  },
  {
    type: "pause_buys",
    label: "Pause buys",
    summary: "Blocks new buy execution while preserving discovery, queueing, and monitoring.",
    confirmation: "Candidates can still flow through the funnel, but execution turns into non-buy paths until resumed.",
  },
  {
    type: "resume_buys",
    label: "Resume buys",
    summary: "Restores buy execution after an operator pause.",
    confirmation: "This re-enables real buy attempts for candidates that pass the funnel and execution guards.",
  },
  {
    type: "reload_model",
    label: "Reload model",
    summary: "Reloads the current model artifacts into memory and reapplies the threshold override logic.",
    confirmation: "This does not retrain. It only refreshes the in-memory model view from artifacts already on disk.",
  },
  {
    type: "trigger_retrain",
    label: "Trigger retrain",
    summary: "Runs the retraining workflow on demand and reloads the model if a better artifact is produced.",
    confirmation: "This can take time and may not publish a new model if the retrain decides not to replace the current one.",
  },
  {
    type: "refresh_reports",
    label: "Refresh reports",
    summary: "Regenerates operator-facing reports and research scorecards from current state.",
    confirmation: "This rebuilds report artifacts on disk. It does not change runtime flags or portfolio state.",
  },
  {
    type: "set_log_level",
    label: "Set log level",
    summary: "Changes the root or named logger level without restarting the bot.",
    confirmation: "This changes runtime log verbosity immediately for the selected logger.",
  },
];


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


function uniqueSources(...groups: SourceStatus[][]) {
  const seen = new Map<string, SourceStatus>();
  groups.flat().forEach((source) => {
    if (!seen.has(source.source_key)) {
      seen.set(source.source_key, source);
    }
  });
  return Array.from(seen.values());
}


function isControlCommandType(value: string | null): value is ControlCommandType {
  return commandOrder.includes(value as ControlCommandType);
}


function commandStatusTone(status: ControlCommandStatus | string | null | undefined) {
  switch (status) {
    case "done":
      return "success";
    case "running":
      return "info";
    case "pending":
    case "rejected":
      return "warn";
    case "failed":
      return "danger";
    default:
      return "neutral";
  }
}


function processStatusTone(status: BotProcessData["status"] | string | null | undefined) {
  switch (status) {
    case "running_managed":
      return "success";
    case "starting":
    case "running_external":
      return "info";
    case "crashed":
      return "danger";
    case "stopped":
      return "neutral";
    default:
      return "warn";
  }
}


function processStatusLabel(status: BotProcessData["status"] | string | null | undefined) {
  switch (status) {
    case "running_managed":
      return "running managed";
    case "running_external":
      return "running external";
    default:
      return humanizeKey(String(status || "unknown"));
  }
}


function commandLabel(commandType: ControlCommandType | string | null | undefined) {
  return commandCatalog.find((item) => item.type === commandType)?.label || humanizeKey(String(commandType || "unknown"));
}


function commandPermission(commandType: ControlCommandType) {
  return `control.command.${commandType}` as const;
}


function prettyJson(value: unknown) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}


function durationSummary(item: ControlCommandItem) {
  if (!item.started_at || !item.finished_at) {
    return item.started_at ? "running" : "queued";
  }
  const started = new Date(item.started_at).getTime();
  const finished = new Date(item.finished_at).getTime();
  if (Number.isNaN(started) || Number.isNaN(finished)) {
    return "n/a";
  }
  const diffMs = Math.max(0, finished - started);
  if (diffMs < 1000) {
    return `${diffMs}ms`;
  }
  const diffSec = diffMs / 1000;
  return diffSec < 10 ? `${diffSec.toFixed(1)}s` : `${Math.round(diffSec)}s`;
}


function resultSummary(item: ControlCommandItem) {
  if (item.error_text) {
    return item.error_text;
  }
  const keys = Object.keys(item.result || {});
  if (!keys.length) {
    return "No structured result";
  }
  return keys.slice(0, 3).join(", ");
}


export function ControlCenterPage() {
  const { openPanel } = useDrawer();
  const { session, hasPermission } = useAuth();
  const { overviewEnvelope } = useOutletContext<ShellOutletContext>();
  const [searchParams, setSearchParams] = useSearchParams();
  const [statusFilter, setStatusFilter] = useState<ControlCommandStatus | "all">("all");
  const [commandFilter, setCommandFilter] = useState<ControlCommandType | "all">("all");
  const [retrainForce, setRetrainForce] = useState(false);
  const [refreshForce, setRefreshForce] = useState(true);
  const [refreshReports, setRefreshReports] = useState<Array<(typeof reportOptions)[number]>>(["baseline", "edge", "research"]);
  const [logLevel, setLogLevel] = useState<(typeof logLevelOptions)[number]>("INFO");
  const [loggerName, setLoggerName] = useState("root");
  const [processDryRun, setProcessDryRun] = useState(true);
  const [processFileLog, setProcessFileLog] = useState(true);
  const [processForceStop, setProcessForceStop] = useState(true);
  const [confirmLiveStart, setConfirmLiveStart] = useState(false);
  const [isConfirmed, setIsConfirmed] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitFeedback, setSubmitFeedback] = useState<{ tone: "success" | "danger"; message: string } | null>(null);
  const [isProcessSubmitting, setIsProcessSubmitting] = useState(false);
  const [processFeedback, setProcessFeedback] = useState<{ tone: "success" | "danger"; message: string } | null>(null);

  const requestedCommand = searchParams.get("command");
  const selectedCommand = isControlCommandType(requestedCommand) ? requestedCommand : "pause_discovery";

  const controlStateQuery = usePollEnvelope<ControlStateData>("/api/v1/control/state", 3000);
  const livePreflightQuery = usePollEnvelope<LivePromotionPreflightData>("/api/v1/control/live-preflight", 5000);
  const historyQuery = usePollEnvelope<ControlCommandsData>(
    buildPath("/api/v1/control/commands", {
      limit: 50,
      status: statusFilter === "all" ? undefined : statusFilter,
      command_type: commandFilter === "all" ? undefined : commandFilter,
    }),
    3000,
  );

  useEffect(() => {
    setIsConfirmed(false);
  }, [selectedCommand, retrainForce, refreshForce, refreshReports, logLevel, loggerName]);

  useEffect(() => {
    setConfirmLiveStart(false);
  }, [processDryRun]);

  const controlState = controlStateQuery.envelope?.data;
  const history = historyQuery.envelope?.data.items || [];
  const sourceStatus = uniqueSources(
    controlStateQuery.envelope?.meta.source_status || [],
    livePreflightQuery.envelope?.meta.source_status || [],
    historyQuery.envelope?.meta.source_status || [],
  );
  const lastCommand = controlState?.commands.last_command || history[0] || null;
  const runtime = controlState?.runtime;
  const processState = controlState?.process;
  const livePreflight = livePreflightQuery.envelope?.data || null;
  const commandDefinition = commandCatalog.find((item) => item.type === selectedCommand) || commandCatalog[0];
  const queryError = controlStateQuery.error || historyQuery.error || livePreflightQuery.error;
  const currentUser = session?.user || null;
  const canQueueSelectedCommand = hasPermission(commandPermission(selectedCommand));
  const canStartProcess = hasPermission("control.process.start");
  const canStopProcess = hasPermission("control.process.stop");

  function selectCommand(nextCommand: ControlCommandType) {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set("command", nextCommand);
    setSearchParams(nextParams, { replace: true });
    setSubmitFeedback(null);
  }

  function toggleReport(reportKey: (typeof reportOptions)[number]) {
    setRefreshReports((current) =>
      current.includes(reportKey)
        ? current.filter((item) => item !== reportKey)
        : [...current, reportKey],
    );
  }

  function currentPayload(): Record<string, unknown> {
    switch (selectedCommand) {
      case "trigger_retrain":
        return { force: retrainForce };
      case "refresh_reports":
        return { force: refreshForce, include: refreshReports };
      case "set_log_level":
        return { level: logLevel, logger: loggerName.trim() || "root" };
      default:
        return {};
    }
  }

  function redundancyReason() {
    if (!runtime) {
      return null;
    }
    if (selectedCommand === "pause_discovery" && runtime.discovery_paused) {
      return "Discovery is already paused.";
    }
    if (selectedCommand === "resume_discovery" && !runtime.discovery_paused) {
      return "Discovery is already live.";
    }
    if (selectedCommand === "pause_buys" && runtime.buys_paused) {
      return "Buys are already paused.";
    }
    if (selectedCommand === "resume_buys" && !runtime.buys_paused) {
      return "Buys are already live.";
    }
    if (selectedCommand === "refresh_reports" && !refreshReports.length) {
      return "Select at least one report lane.";
    }
    return null;
  }

  function availabilityLabel(commandType: ControlCommandType) {
    if (!hasPermission(commandPermission(commandType))) {
      return "role locked";
    }
    if (!runtime) {
      return "Awaiting runtime";
    }
    if (commandType === "pause_discovery") {
      return runtime.discovery_paused ? "already paused" : "ready";
    }
    if (commandType === "resume_discovery") {
      return runtime.discovery_paused ? "ready" : "already live";
    }
    if (commandType === "pause_buys") {
      return runtime.buys_paused ? "already paused" : "ready";
    }
    if (commandType === "resume_buys") {
      return runtime.buys_paused ? "ready" : "already live";
    }
    return "ready";
  }

  function applySavedView(filters: Record<string, unknown>) {
    if (typeof filters.selectedCommand === "string" && isControlCommandType(filters.selectedCommand)) {
      selectCommand(filters.selectedCommand);
    }
    setStatusFilter(typeof filters.statusFilter === "string" ? (filters.statusFilter as ControlCommandStatus | "all") : "all");
    setCommandFilter(typeof filters.commandFilter === "string" && isControlCommandType(filters.commandFilter) ? filters.commandFilter : "all");
    setRetrainForce(Boolean(filters.retrainForce));
    setRefreshForce(typeof filters.refreshForce === "boolean" ? filters.refreshForce : true);
    setRefreshReports(Array.isArray(filters.refreshReports) ? filters.refreshReports.filter((item): item is (typeof reportOptions)[number] => reportOptions.includes(item as (typeof reportOptions)[number])) : ["baseline", "edge", "research"]);
    setLogLevel(typeof filters.logLevel === "string" && logLevelOptions.includes(filters.logLevel as (typeof logLevelOptions)[number]) ? (filters.logLevel as (typeof logLevelOptions)[number]) : "INFO");
    setLoggerName(typeof filters.loggerName === "string" ? filters.loggerName : "root");
    setProcessDryRun(typeof filters.processDryRun === "boolean" ? filters.processDryRun : true);
    setProcessFileLog(typeof filters.processFileLog === "boolean" ? filters.processFileLog : true);
    setProcessForceStop(typeof filters.processForceStop === "boolean" ? filters.processForceStop : true);
    setConfirmLiveStart(false);
  }

  async function submitCommand() {
    const blockedReason = redundancyReason();
    if (blockedReason) {
      setSubmitFeedback({ tone: "danger", message: blockedReason });
      return;
    }
    if (!currentUser) {
      setSubmitFeedback({ tone: "danger", message: "Authenticated user is required." });
      return;
    }
    if (!canQueueSelectedCommand) {
      setSubmitFeedback({ tone: "danger", message: "Your current role cannot queue this command." });
      return;
    }

    const request: ControlCommandCreateRequest = {
      bot_id: "main",
      command_type: selectedCommand,
      payload: currentPayload(),
      requested_from: "ui",
      idempotency_key: `ui-${selectedCommand}-${Date.now()}`,
    };

    setIsSubmitting(true);
    setSubmitFeedback(null);
    try {
      const envelope = await postEnvelope<ControlCommandCreateData, ControlCommandCreateRequest>(
        "/api/v1/control/commands",
        request,
      );
      setSubmitFeedback({
        tone: "success",
        message: `Queued ${commandDefinition.label} as command #${envelope.data.id}.`,
      });
      setIsConfirmed(false);
      controlStateQuery.refetch();
      historyQuery.refetch();
    } catch (error) {
      setSubmitFeedback({
        tone: "danger",
        message: error instanceof Error ? error.message : "Unknown command submission failure",
      });
    } finally {
      setIsSubmitting(false);
    }
  }

  async function submitProcessAction(action: "start" | "stop") {
    if (!currentUser) {
      setProcessFeedback({ tone: "danger", message: "Authenticated user is required." });
      return;
    }
    if (action === "start" && !canStartProcess) {
      setProcessFeedback({ tone: "danger", message: "Your current role cannot start the bot process." });
      return;
    }
    if (action === "stop" && !canStopProcess) {
      setProcessFeedback({ tone: "danger", message: "Your current role cannot stop the bot process." });
      return;
    }

    setIsProcessSubmitting(true);
    setProcessFeedback(null);
    try {
      if (action === "start") {
        if (!processDryRun && !livePreflight?.passed) {
          setProcessFeedback({ tone: "danger", message: "Live preflight is not passing yet." });
          return;
        }
        if (!processDryRun && !confirmLiveStart) {
          setProcessFeedback({ tone: "danger", message: "Live start requires explicit confirmation." });
          return;
        }
        await postEnvelope<BotProcessData, BotProcessStartRequest>("/api/v1/control/process/start", {
          bot_id: "main",
          dry_run: processDryRun,
          file_log: processFileLog,
          confirm_live: !processDryRun && confirmLiveStart,
          requested_from: "ui",
        });
        setProcessFeedback({
          tone: "success",
          message: `Bot launch requested in ${processDryRun ? "dry-run" : "real"} mode.`,
        });
      } else {
        await postEnvelope<BotProcessData, BotProcessStopRequest>("/api/v1/control/process/stop", {
          bot_id: "main",
          force: processForceStop,
        });
        setProcessFeedback({
          tone: "success",
          message: "Bot stop requested for the UI-managed process tree.",
        });
      }
      controlStateQuery.refetch();
      historyQuery.refetch();
    } catch (error) {
      setProcessFeedback({
        tone: "danger",
        message: error instanceof Error ? error.message : "Unknown bot process action failure",
      });
    } finally {
      setIsProcessSubmitting(false);
    }
  }

  function openProcessDrawer(process: BotProcessData) {
    openPanel({
      eyebrow: "Control / process detail",
      title: `Bot process: ${processStatusLabel(process.status)}`,
      description: process.detail,
      content: (
        <div className="drawer-stack">
          {Object.entries(process).map(([key, value]) => (
            <div className="drawer-kv" key={`process-${key}`}>
              <strong>{humanizeKey(key)}</strong>
              <span>{typeof value === "object" ? prettyJson(value) : String(value ?? "n/a")}</span>
            </div>
          ))}
        </div>
      ),
    });
  }

  function openCommandDrawer(item: ControlCommandItem) {
    openPanel({
      eyebrow: "Control / command detail",
      title: `${commandLabel(item.command_type)} #${item.id}`,
      description: `${item.status} · requested ${formatTimestamp(item.requested_at)}`,
      content: (
        <div className="drawer-stack">
          <div className="drawer-kv">
            <strong>Requested by</strong>
            <span>{item.requested_by || "n/a"}</span>
          </div>
          <div className="drawer-kv">
            <strong>Requested from</strong>
            <span>{item.requested_from || "n/a"}</span>
          </div>
          <div className="drawer-kv">
            <strong>Started</strong>
            <span>{formatTimestamp(item.started_at)}</span>
          </div>
          <div className="drawer-kv">
            <strong>Finished</strong>
            <span>{formatTimestamp(item.finished_at)}</span>
          </div>
          <div className="drawer-kv">
            <strong>Duration</strong>
            <span>{durationSummary(item)}</span>
          </div>
          <div className="drawer-stack">
            <strong>Payload</strong>
            <pre className="json-block">{prettyJson(item.payload)}</pre>
          </div>
          <div className="drawer-stack">
            <strong>Result</strong>
            <pre className="json-block">{prettyJson(item.result)}</pre>
          </div>
          {item.error_text ? (
            <div className="drawer-stack">
              <strong>Error</strong>
              <pre className="json-block json-block--danger">{item.error_text}</pre>
            </div>
          ) : null}
        </div>
      ),
    });
  }

  function commandColumns(): DataColumn<ControlCommandItem>[] {
    return [
      {
        id: "command",
        header: "Command",
        render: (row) => (
          <button className="mono-link-button table-primary-cell" onClick={() => openCommandDrawer(row)} type="button">
            <strong>{commandLabel(row.command_type)}</strong>
            <small>#{row.id} · {row.bot_id}</small>
          </button>
        ),
      },
      {
        id: "requested",
        header: "Requested",
        render: (row) => (
          <div className="table-primary-cell">
            <strong>{formatTimestamp(row.requested_at)}</strong>
            <small>{formatRelative(row.requested_at)} ago</small>
          </div>
        ),
      },
      {
        id: "operator",
        header: "Operator",
        render: (row) => row.requested_by || "n/a",
      },
      {
        id: "status",
        header: "Status",
        render: (row) => <StatusChip compact label={row.status} mono tone={commandStatusTone(row.status)} />,
      },
      {
        id: "duration",
        align: "right",
        header: "Duration",
        render: (row) => durationSummary(row),
      },
      {
        id: "result",
        header: "Result",
        render: (row) => resultSummary(row),
      },
    ];
  }

  if (!controlStateQuery.envelope && !historyQuery.envelope && !queryError) {
    return (
      <Surface eyebrow="Operate / control" title="Control Center" subtitle="Waiting for the first control payloads">
        <p>The page is polling `/api/v1/control/state` and `/api/v1/control/commands`.</p>
      </Surface>
    );
  }

  return (
    <div className="page-stack">
      <PageHero
        actions={
          <div className="page-hero__actions-inline">
            <button className="ui-button ui-button--ghost" onClick={() => controlStateQuery.refetch()} type="button">
              Refresh state
            </button>
            <button className="ui-button ui-button--ghost" onClick={() => historyQuery.refetch()} type="button">
              Refresh history
            </button>
            {lastCommand ? (
              <button className="ui-button ui-button--ghost" onClick={() => openCommandDrawer(lastCommand)} type="button">
                Open last command
              </button>
            ) : null}
          </div>
        }
        eyebrow="Operate / command plane"
        meta={
          <>
            <StatusChip
              label={controlStateQuery.envelope?.meta.degraded ? "control degraded" : "control live"}
              tone={controlStateQuery.envelope?.meta.degraded ? "warn" : "success"}
            />
            {processState ? (
              <StatusChip label={processStatusLabel(processState.status)} tone={processStatusTone(processState.status)} />
            ) : null}
            <StatusChip
              label={
                runtime?.discovery_paused === null || runtime?.discovery_paused === undefined
                  ? "discovery offline"
                  : runtime.discovery_paused
                    ? "discovery paused"
                    : "discovery live"
              }
              tone={
                runtime?.discovery_paused === null || runtime?.discovery_paused === undefined
                  ? "neutral"
                  : runtime.discovery_paused
                    ? "warn"
                    : "success"
              }
            />
            <StatusChip
              label={
                runtime?.buys_paused === null || runtime?.buys_paused === undefined
                  ? "buys offline"
                  : runtime.buys_paused
                    ? "buys paused"
                    : "buys live"
              }
              tone={
                runtime?.buys_paused === null || runtime?.buys_paused === undefined
                  ? "neutral"
                  : runtime.buys_paused
                    ? "warn"
                    : "success"
              }
            />
            <StatusChip label={`${formatCount(controlState?.commands.pending_count)} pending`} tone="info" compact />
            <StatusChip label={`${formatCount(controlState?.commands.running_count)} running`} tone="info" compact />
          </>
        }
        question="What should the operator change right now, and how will that decision stay auditable?"
        summary="Control Center is now live over the persisted command bus: runtime posture, guarded command submission, and append-only audit history all sit on the same page."
        title="Control Center"
      />

      {controlStateQuery.envelope?.meta.degraded ? (
        <Banner
          detail="Runtime posture or command sources are degraded. Commands can still be queued, but the current bot posture may not be fresh."
          title="Control plane degraded"
          tone="warn"
        />
      ) : null}

      {controlStateQuery.envelope?.meta.stale ? (
        <Banner
          detail={`The control posture is stale. Latest runtime heartbeat: ${formatTimestamp(runtime?.heartbeat_at || null)}.`}
          title="Control posture stale"
          tone="warn"
        />
      ) : null}

      {submitFeedback ? (
        <Banner
          detail={submitFeedback.message}
          title={submitFeedback.tone === "success" ? "Command queued" : "Command not queued"}
          tone={submitFeedback.tone}
        />
      ) : null}

      {processFeedback ? (
        <Banner
          detail={processFeedback.message}
          title={processFeedback.tone === "success" ? "Process action queued" : "Process action failed"}
          tone={processFeedback.tone}
        />
      ) : null}

      {runtime?.last_error ? (
        <Banner
          detail={runtime.last_error}
          title="Runtime reported last error"
          tone="danger"
        />
      ) : null}

      {processState?.status === "crashed" ? (
        <Banner
          detail={processState.detail}
          title="Managed bot process exited"
          tone="danger"
        />
      ) : null}

      {queryError ? (
        <Banner
          detail={queryError}
          title="Control query failed"
          tone="danger"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface className="grid-span-4" eyebrow="Runtime posture" title={runtime?.process_state || "Unknown process state"} subtitle={`Heartbeat ${formatTimestamp(runtime?.heartbeat_at || null)}`}>
          <div className="metric-ribbon">
            <div className="metric-ribbon__item">
              <span>Bot</span>
              <strong>{controlState?.bot_id || "main"}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Runtime staleness</span>
              <strong>{runtime?.staleness || "n/a"}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Heartbeat age</span>
              <strong>{runtime?.heartbeat_age_s !== null && runtime?.heartbeat_age_s !== undefined ? `${runtime.heartbeat_age_s}s` : "n/a"}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Retrain state</span>
              <strong>{runtime?.retrain_state || "idle"}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Reports refresh</span>
              <strong>{runtime?.reports_refresh_state || "idle"}</strong>
            </div>
          </div>
        </Surface>

        <Surface
          className="grid-span-4"
          eyebrow="Process control"
          title={processState ? processStatusLabel(processState.status) : "Bot process manager"}
          subtitle={processState?.detail || "UI-managed launch and stop controls"}
        >
          <div className="control-form">
            <div className="kv-grid">
              <div className="kv-cell">
                <span>PID</span>
                <strong>{processState?.pid || "n/a"}</strong>
              </div>
              <div className="kv-cell">
                <span>Mode</span>
                <strong>
                  {processState?.dry_run === null || processState?.dry_run === undefined
                    ? "n/a"
                    : processState.dry_run
                      ? "dry-run"
                      : "real"}
                </strong>
              </div>
              <div className="kv-cell">
                <span>Owner</span>
                <strong>{processState?.managed ? "UI managed" : processState?.external ? "external" : "stopped"}</strong>
              </div>
            </div>

            <div className="drawer-note">
              <strong>Launch policy</strong>
              <p>
                `start_stack.ps1` now leaves the bot stopped by default. Start and stop from here only manages the UI-owned
                process; a bot launched manually from console remains external.
              </p>
            </div>

            <div className="checkbox-grid">
              <label className="checkbox-chip">
                <input checked={processDryRun} onChange={(event) => setProcessDryRun(event.target.checked)} type="checkbox" />
                <span>Start in dry-run mode</span>
              </label>
              <label className="checkbox-chip">
                <input checked={processFileLog} onChange={(event) => setProcessFileLog(event.target.checked)} type="checkbox" />
                <span>Enable file logging</span>
              </label>
              <label className="checkbox-chip">
                <input checked={processForceStop} onChange={(event) => setProcessForceStop(event.target.checked)} type="checkbox" />
                <span>Force stop managed tree</span>
              </label>
            </div>

            {!processDryRun ? (
              <div className="control-form">
                <Banner
                  detail={
                    livePreflight?.passed
                      ? `Live profile can be generated at ${livePreflight.profile_path}.`
                      : "The bot will stay in paper acquisition until every live gate passes."
                  }
                  title={livePreflight?.passed ? "Live preflight passed" : "Live preflight blocked"}
                  tone={livePreflight?.passed ? "success" : "warn"}
                />
                <div className="kv-grid">
                  {(livePreflight?.gates || []).map((gate) => (
                    <div className="kv-cell" key={gate.id}>
                      <span>{gate.label}</span>
                      <strong>
                        <StatusChip
                          compact
                          label={gate.status}
                          tone={gate.status === "pass" ? "success" : "danger"}
                        />
                      </strong>
                      <small>{gate.detail}</small>
                    </div>
                  ))}
                </div>
                <label className="checkbox-chip">
                  <input
                    checked={confirmLiveStart}
                    disabled={!livePreflight?.passed}
                    onChange={(event) => setConfirmLiveStart(event.target.checked)}
                    type="checkbox"
                  />
                  <span>I confirm this UI-managed start should use the generated live profile.</span>
                </label>
              </div>
            ) : null}

            {!canStartProcess || !canStopProcess ? (
              <Banner
                detail="Only roles with explicit process permissions can launch or stop the bot from the UI."
                title="Role restriction"
                tone="warn"
              />
            ) : null}

            {processState?.external ? (
              <Banner
                detail="The current bot heartbeat comes from a manual console launch. Stop it from that console before switching to UI-managed orchestration."
                title="External bot detected"
                tone="info"
              />
            ) : null}

            <div className="page-hero__actions-inline">
              <button
                className="ui-button ui-button--primary"
                disabled={
                  isProcessSubmitting
                  || !canStartProcess
                  || !processState?.can_start
                  || (!processDryRun && (!livePreflight?.passed || !confirmLiveStart))
                }
                onClick={() => void submitProcessAction("start")}
                type="button"
              >
                {isProcessSubmitting && processState?.can_start ? "Starting..." : "Start bot"}
              </button>
              <button
                className="ui-button ui-button--ghost"
                disabled={isProcessSubmitting || !canStopProcess || !processState?.can_stop}
                onClick={() => void submitProcessAction("stop")}
                type="button"
              >
                {isProcessSubmitting && processState?.can_stop ? "Stopping..." : "Stop bot"}
              </button>
              {processState ? (
                <button className="ui-button ui-button--ghost" onClick={() => openProcessDrawer(processState)} type="button">
                  Open detail
                </button>
              ) : null}
            </div>
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Source truth" title="Control provenance">
          <SourceHealthStrip sources={sourceStatus} />
        </Surface>

        <Surface className="grid-span-12" eyebrow="Command set" title="Supported actions" subtitle="Every action below maps directly to a persisted command type in the backend.">
          <div className="command-grid">
            {commandCatalog.map((item) => {
              const isActive = item.type === selectedCommand;
              const availability = availabilityLabel(item.type);
              return (
                <article className={["command-card", isActive ? "command-card--active" : ""].filter(Boolean).join(" ")} key={item.type}>
                  <div className="command-card__header">
                    <div>
                      <p className="surface__eyebrow">{item.type}</p>
                      <h3>{item.label}</h3>
                    </div>
                    <StatusChip compact label={availability} tone={availability === "ready" ? "success" : "warn"} />
                  </div>
                  <p>{item.summary}</p>
                  <div className="command-card__footer">
                    <small>{item.confirmation}</small>
                    <button className="ui-button ui-button--ghost" onClick={() => selectCommand(item.type)} type="button">
                      {isActive ? "Selected" : "Prepare"}
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        </Surface>

        <Surface className="grid-span-8" eyebrow="Composer" title={commandDefinition.label} subtitle={commandDefinition.summary}>
          <div className="control-form">
            {selectedCommand === "trigger_retrain" ? (
              <label className="checkbox-chip">
                <input checked={retrainForce} onChange={(event) => setRetrainForce(event.target.checked)} type="checkbox" />
                <span>Mark force request in payload</span>
              </label>
            ) : null}

            {selectedCommand === "refresh_reports" ? (
              <>
                <label className="checkbox-chip">
                  <input checked={refreshForce} onChange={(event) => setRefreshForce(event.target.checked)} type="checkbox" />
                  <span>Force refresh request</span>
                </label>
                <div className="filter-field">
                  <span>Included report lanes</span>
                  <div className="checkbox-grid">
                    {reportOptions.map((option) => (
                      <label className="checkbox-chip" key={option}>
                        <input
                          checked={refreshReports.includes(option)}
                          onChange={() => toggleReport(option)}
                          type="checkbox"
                        />
                        <span>{option}</span>
                      </label>
                    ))}
                  </div>
                </div>
              </>
            ) : null}

            {selectedCommand === "set_log_level" ? (
              <div className="filter-row">
                <label className="filter-field">
                  <span>Log level</span>
                  <select className="ui-field" onChange={(event) => setLogLevel(event.target.value as (typeof logLevelOptions)[number])} value={logLevel}>
                    {logLevelOptions.map((option) => (
                      <option key={option} value={option}>
                        {option}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="filter-field">
                  <span>Logger</span>
                  <input
                    className="ui-field"
                    onChange={(event) => setLoggerName(event.target.value)}
                    placeholder="root"
                    type="text"
                    value={loggerName}
                  />
                </label>
              </div>
            ) : null}

            {!["trigger_retrain", "refresh_reports", "set_log_level"].includes(selectedCommand) ? (
              <p className="empty-note">This command has no additional payload fields in v1.</p>
            ) : null}
          </div>
        </Surface>

        <Surface className="grid-span-4" eyebrow="Review and queue" title="Confirmation gate" subtitle="Commands are persisted first and executed asynchronously by the bot.">
          <div className="control-review">
            <div className="kv-grid">
              <div className="kv-cell">
                <span>Target bot</span>
                <strong>main</strong>
              </div>
              <div className="kv-cell">
                <span>Command</span>
                <strong>{commandDefinition.label}</strong>
              </div>
              <div className="kv-cell">
                <span>Session user</span>
                <strong>{currentUser ? `${currentUser.username} (${currentUser.role})` : "n/a"}</strong>
              </div>
            </div>

            <div className="drawer-note">
              <strong>Confirmation note</strong>
              <p>{commandDefinition.confirmation}</p>
            </div>

            <pre className="json-block">{prettyJson(currentPayload())}</pre>

            {redundancyReason() ? (
              <Banner detail={redundancyReason() || ""} title="Command would be redundant" tone="warn" />
            ) : null}

            {!canQueueSelectedCommand ? (
              <Banner detail="Your current role can inspect control history but cannot queue this command type." title="Role restriction" tone="warn" />
            ) : null}

            <label className="checkbox-chip">
              <input checked={isConfirmed} onChange={(event) => setIsConfirmed(event.target.checked)} type="checkbox" />
              <span>I have reviewed the operator impact and want to queue this command.</span>
            </label>

            <button
              className="ui-button ui-button--primary"
              disabled={isSubmitting || !isConfirmed || !currentUser || !canQueueSelectedCommand || Boolean(redundancyReason())}
              onClick={() => void submitCommand()}
              type="button"
            >
              {isSubmitting ? "Queueing..." : "Queue command"}
            </button>
          </div>
        </Surface>

        <Surface className="grid-span-12" eyebrow="Audit filters" title="History controls" subtitle="Status and command filters are executed against the backend history endpoint.">
          <div className="filter-stack">
            <div className="filter-field">
              <span>Status</span>
              <div className="choice-row">
                {statusOptions.map((option) => (
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

            <div className="filter-field">
              <span>Command type</span>
              <div className="choice-row">
                <button
                  className={["choice-chip", commandFilter === "all" ? "choice-chip--active" : ""].filter(Boolean).join(" ")}
                  onClick={() => setCommandFilter("all")}
                  type="button"
                >
                  all
                </button>
                {commandCatalog.map((item) => (
                  <button
                    className={["choice-chip", commandFilter === item.type ? "choice-chip--active" : ""].filter(Boolean).join(" ")}
                    key={item.type}
                    onClick={() => setCommandFilter(item.type)}
                    type="button"
                  >
                    {item.type}
                  </button>
                ))}
              </div>
            </div>

            <SavedViewsToolbar
              currentFilters={{
                selectedCommand,
                statusFilter,
                commandFilter,
                retrainForce,
                refreshForce,
                refreshReports,
                logLevel,
                loggerName,
                processDryRun,
                processFileLog,
                processForceStop,
                confirmLiveStart,
              }}
              onApply={applySavedView}
              pageKey="control"
            />
          </div>
        </Surface>

        <Surface className="grid-span-8" eyebrow="Audit trail" title="Command history" subtitle="Append-only record of queued, running, completed, rejected, and failed actions.">
          <DataTable
            columns={commandColumns()}
            emptyMessage="No commands match the active filter set."
            rowKey={(row) => `${row.id}`}
            rows={history}
          />
        </Surface>

        <Surface className="grid-span-4" eyebrow="Latest command" title={lastCommand ? commandLabel(lastCommand.command_type) : "No command yet"} subtitle={lastCommand ? `Requested ${formatRelative(lastCommand.requested_at)} ago` : "Queue the first command from this page."}>
          {lastCommand ? (
            <div className="control-latest">
              <div className="drawer-kv">
                <strong>Status</strong>
                <StatusChip compact label={lastCommand.status} mono tone={commandStatusTone(lastCommand.status)} />
              </div>
              <div className="drawer-kv">
                <strong>Operator</strong>
                <span>{lastCommand.requested_by || "n/a"}</span>
              </div>
              <div className="drawer-kv">
                <strong>Duration</strong>
                <span>{durationSummary(lastCommand)}</span>
              </div>
              <div className="drawer-kv">
                <strong>Result summary</strong>
                <span>{resultSummary(lastCommand)}</span>
              </div>
              <button className="ui-button ui-button--ghost" onClick={() => openCommandDrawer(lastCommand)} type="button">
                Open detail
              </button>
            </div>
          ) : (
            <p className="empty-note">No command has been queued yet for `main`.</p>
          )}
        </Surface>
      </div>

      {overviewEnvelope?.data ? (
        <Surface eyebrow="Cross-surface context" title="Overview alignment" subtitle="The control plane and the shell header are reading the same persisted runtime posture.">
          <div className="metric-ribbon">
            <div className="metric-ribbon__item">
              <span>Overview process</span>
              <strong>{overviewEnvelope.data.bot.process_state || "n/a"}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Overview staleness</span>
              <strong>{overviewEnvelope.data.bot.staleness || "n/a"}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Queue pending</span>
              <strong>{formatCount(overviewEnvelope.data.queue.pending)}</strong>
            </div>
            <div className="metric-ribbon__item">
              <span>Open positions</span>
              <strong>{formatCount(overviewEnvelope.data.positions.open_rows)}</strong>
            </div>
          </div>
        </Surface>
      ) : null}
    </div>
  );
}
