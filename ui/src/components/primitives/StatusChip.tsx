import type { SourceState } from "../../lib/api";


type ChipTone = "neutral" | "success" | "warn" | "danger" | "info";

interface StatusChipProps {
  label: string;
  tone?: ChipTone;
  compact?: boolean;
  mono?: boolean;
}


export function toneFromStatus(
  status: SourceState | "fresh" | "degraded" | "running" | "paused" | "stopped" | "off" | "starting" | "running_external" | "running_managed" | "crashed",
) {
  switch (status) {
    case "ok":
    case "fresh":
    case "running":
    case "running_managed":
      return "success";
    case "starting":
    case "running_external":
      return "info";
    case "stale":
    case "empty":
    case "paused":
      return "warn";
    case "error":
    case "missing":
    case "degraded":
    case "crashed":
      return "danger";
    case "stopped":
    case "off":
      return "neutral";
    default:
      return "info";
  }
}


export function StatusChip({ label, tone = "neutral", compact = false, mono = false }: StatusChipProps) {
  return (
    <span
      className={[
        "status-chip",
        `status-chip--${tone}`,
        compact ? "status-chip--compact" : "",
        mono ? "status-chip--mono" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <span className="status-chip__dot" />
      <span>{label}</span>
    </span>
  );
}
