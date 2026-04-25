import { useDeferredValue, useState } from "react";

import { useDrawer } from "../app/drawer";
import { Banner } from "../components/primitives/Banner";
import { PageHero } from "../components/primitives/PageHero";
import { SavedViewsToolbar } from "../components/primitives/SavedViewsToolbar";
import { SourceHealthStrip } from "../components/primitives/SourceHealthStrip";
import { StatusChip } from "../components/primitives/StatusChip";
import { Surface } from "../components/primitives/Surface";
import { TimelineRail } from "../components/primitives/TimelineRail";
import { usePollEnvelope } from "../hooks/usePollEnvelope";
import type {
  LogsTailData,
  ResearchEventsData,
  RuntimeEventItem,
  RuntimeEventsData,
} from "../lib/api";
import { formatCount, formatRelative, formatTimestamp, humanizeKey } from "../lib/format";


const tailTargets = ["app", "runtime_events", "research_events"];
const lineOptions = [40, 80, 120];


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


export function LogsPage() {
  const { openPanel } = useDrawer();
  const [tailTarget, setTailTarget] = useState("app");
  const [lineCount, setLineCount] = useState(80);
  const [addressFilter, setAddressFilter] = useState("");
  const deferredAddress = useDeferredValue(addressFilter.trim());

  const tailQuery = usePollEnvelope<LogsTailData>(
    buildPath("/api/v1/logs/tail", {
      target: tailTarget,
      lines: lineCount,
    }),
    2500,
  );
  const runtimeEventsQuery = usePollEnvelope<RuntimeEventsData>(
    buildPath("/api/v1/events/runtime", {
      limit: Math.min(lineCount, 24),
      address: deferredAddress || undefined,
    }),
    2500,
  );
  const researchEventsQuery = usePollEnvelope<ResearchEventsData>(
    buildPath("/api/v1/events/research", {
      limit: Math.min(lineCount, 24),
      address: deferredAddress || undefined,
    }),
    2500,
  );

  const queryError = tailQuery.error || runtimeEventsQuery.error || researchEventsQuery.error;
  const sourceStatus = Array.from(
    new Map(
      [
        ...(tailQuery.envelope?.meta.source_status || []),
        ...(runtimeEventsQuery.envelope?.meta.source_status || []),
        ...(researchEventsQuery.envelope?.meta.source_status || []),
      ].map((status) => [status.source_key, status]),
    ).values(),
  );

  function openEventDrawer(stream: string, item: RuntimeEventItem) {
    openPanel({
      eyebrow: `Logs / ${stream}`,
      title: item.summary,
      description: `${item.event_type} | ${formatTimestamp(item.ts_utc)}`,
      content: (
        <div className="drawer-stack">
          <div className="drawer-kv">
            <strong>Address</strong>
            <span>{item.address || "global event"}</span>
          </div>
          {Object.entries(item.payload).map(([key, value]) => (
            <div className="drawer-kv" key={`${stream}-${item.id}-${key}`}>
              <strong>{humanizeKey(key)}</strong>
              <span>{stringifyValue(value)}</span>
            </div>
          ))}
        </div>
      ),
    });
  }

  function applySavedView(filters: Record<string, unknown>) {
    setTailTarget(typeof filters.tailTarget === "string" ? filters.tailTarget : "app");
    setLineCount(typeof filters.lineCount === "number" ? filters.lineCount : 80);
    setAddressFilter(typeof filters.addressFilter === "string" ? filters.addressFilter : "");
  }

  if (!tailQuery.envelope && !runtimeEventsQuery.envelope && !researchEventsQuery.envelope && !queryError) {
    return (
      <Surface eyebrow="Inspect / logs" title="Logs and events" subtitle="Waiting for the first log and event payloads">
        <p>The page is polling `/api/v1/logs/tail`, `/api/v1/events/runtime`, and `/api/v1/events/research`.</p>
      </Surface>
    );
  }

  return (
    <div className="page-stack">
      <PageHero
        eyebrow="Inspect / traces"
        meta={
          <>
            <StatusChip label={tailTarget} tone="info" compact mono />
            <StatusChip
              label={tailQuery.envelope?.meta.degraded ? "tail degraded" : "tail live"}
              tone={tailQuery.envelope?.meta.degraded ? "warn" : "success"}
            />
            <StatusChip label={`${formatCount(tailQuery.envelope?.data.count)} lines`} tone="neutral" compact />
            {deferredAddress ? <StatusChip label={`address ${deferredAddress}`} tone="info" compact mono /> : null}
          </>
        }
        question="What happened most recently, in exact chronological terms?"
        summary="Logs and Events now combines the raw tail with normalized runtime and research rails so the operator can move between text traces and structured events without leaving the shell."
        title="Logs and events"
      />

      {tailQuery.envelope?.meta.degraded || runtimeEventsQuery.envelope?.meta.degraded || researchEventsQuery.envelope?.meta.degraded ? (
        <Banner
          detail="At least one trace source is degraded. The page keeps rendering the remaining rails and exposes the exact failing source in the provenance strip."
          title="Trace source degraded"
          tone="warn"
        />
      ) : null}

      {queryError ? (
        <Banner
          detail={queryError}
          title="Trace query failed"
          tone="danger"
        />
      ) : null}

      <div className="editorial-grid">
        <Surface className="grid-span-12" eyebrow="Controls" title="Tail target and filters" subtitle="Raw tail is switched independently from the normalized event rails.">
          <div className="filter-stack">
            <div className="filter-field">
              <span>Tail target</span>
              <div className="choice-row">
                {tailTargets.map((option) => (
                  <button
                    className={["choice-chip", tailTarget === option ? "choice-chip--active" : ""].filter(Boolean).join(" ")}
                    key={option}
                    onClick={() => setTailTarget(option)}
                    type="button"
                  >
                    {option}
                  </button>
                ))}
              </div>
            </div>

            <div className="filter-row">
              <label className="filter-field">
                <span>Line count</span>
                <select
                  className="ui-field"
                  onChange={(event) => setLineCount(Number(event.target.value))}
                  value={lineCount}
                >
                  {lineOptions.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>

              <label className="filter-field">
                <span>Address filter for event rails</span>
                <input
                  className="ui-field"
                  onChange={(event) => setAddressFilter(event.target.value)}
                  placeholder="token address"
                  type="search"
                  value={addressFilter}
                />
              </label>
            </div>

            <SavedViewsToolbar
              currentFilters={{ tailTarget, lineCount, addressFilter }}
              onApply={applySavedView}
              pageKey="logs"
            />
          </div>
        </Surface>

        <Surface
          className="grid-span-12"
          eyebrow="Raw tail"
          title={tailQuery.envelope?.data.target || tailTarget}
          subtitle={tailQuery.envelope?.data.path || "Waiting for tail path"}
        >
          <div className="log-tail">
            {(tailQuery.envelope?.data.lines || []).map((line, index) => (
              <div className="log-tail__line" key={`${tailQuery.envelope?.data.target || tailTarget}-${index}`}>
                <span className="log-tail__index">{String(index + 1).padStart(3, "0")}</span>
                <code className="log-tail__text">{line}</code>
              </div>
            ))}
            {!tailQuery.envelope?.data.lines?.length ? <p className="empty-note">No lines available for the selected target.</p> : null}
          </div>
        </Surface>

        <Surface
          className="grid-span-6"
          eyebrow="Runtime rail"
          title="Normalized runtime events"
          subtitle={runtimeEventsQuery.envelope?.data.items[0] ? `Latest ${formatRelative(runtimeEventsQuery.envelope.data.items[0].ts_utc)} ago` : "No runtime events"}
        >
          <TimelineRail
            emptyMessage="No runtime events available."
            items={runtimeEventsQuery.envelope?.data.items || []}
            onSelect={(item) => openEventDrawer("runtime", item)}
          />
        </Surface>

        <Surface
          className="grid-span-6"
          eyebrow="Research rail"
          title="Normalized research events"
          subtitle={researchEventsQuery.envelope?.data.items[0] ? `Latest ${formatRelative(researchEventsQuery.envelope.data.items[0].ts_utc)} ago` : "No research events"}
        >
          <TimelineRail
            emptyMessage="No research events available."
            items={researchEventsQuery.envelope?.data.items || []}
            onSelect={(item) => openEventDrawer("research", item)}
          />
        </Surface>

        <Surface className="grid-span-12" eyebrow="Source truth" title="Trace provenance">
          <SourceHealthStrip sources={sourceStatus} />
        </Surface>
      </div>
    </div>
  );
}
