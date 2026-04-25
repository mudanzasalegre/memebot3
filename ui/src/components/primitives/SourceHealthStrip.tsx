import type { SourceStatus } from "../../lib/api";
import { formatRelative, humanizeKey } from "../../lib/format";
import { StatusChip, toneFromStatus } from "./StatusChip";


interface SourceHealthStripProps {
  sources: SourceStatus[];
}


const priorityKeys = [
  "api.auth",
  "sqlite.main",
  "sqlite.bot_runtime_state",
  "runtime.bot_process_manager",
  "sqlite.control_commands",
  "sqlite.ui_saved_views",
  "metrics.runtime_events",
  "metrics.research_events",
  "features.latest_parquet",
];


export function SourceHealthStrip({ sources }: SourceHealthStripProps) {
  const ordered = [...sources].sort((left, right) => {
    const leftIndex = priorityKeys.indexOf(left.source_key);
    const rightIndex = priorityKeys.indexOf(right.source_key);
    const safeLeft = leftIndex === -1 ? Number.MAX_SAFE_INTEGER : leftIndex;
    const safeRight = rightIndex === -1 ? Number.MAX_SAFE_INTEGER : rightIndex;
    return safeLeft - safeRight;
  });

  return (
    <div className="source-strip">
      {ordered.map((source) => (
        <div className="source-strip__item" key={source.source_key}>
          <div className="source-strip__label">
            <span>{humanizeKey(source.source_key)}</span>
            <StatusChip label={source.status} tone={toneFromStatus(source.status)} compact mono />
          </div>
          <small>{source.updated_at ? `${formatRelative(source.updated_at)} ago` : source.detail || "No timestamp"}</small>
        </div>
      ))}
    </div>
  );
}
