const shortNumber = new Intl.NumberFormat("en", {
  notation: "compact",
  maximumFractionDigits: 1,
});

const integerNumber = new Intl.NumberFormat("en", {
  maximumFractionDigits: 0,
});

const decimalNumber = new Intl.NumberFormat("en", {
  minimumFractionDigits: 0,
  maximumFractionDigits: 3,
});

const moneyNumber = new Intl.NumberFormat("en", {
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
});

const shortTime = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  month: "short",
  day: "2-digit",
  year: "numeric",
  timeZoneName: "short",
});


export function formatTimestamp(value: string | null | undefined) {
  if (!value) {
    return "No timestamp";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return shortTime.format(date);
}


export function formatRelative(value: string | null | undefined) {
  if (!value) {
    return "never";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "unknown";
  }

  const diffMs = Date.now() - date.getTime();
  const diffSec = Math.round(diffMs / 1000);
  const abs = Math.abs(diffSec);
  if (abs < 60) {
    return `${diffSec}s`;
  }
  const diffMin = Math.round(diffSec / 60);
  if (Math.abs(diffMin) < 60) {
    return `${diffMin}m`;
  }
  const diffH = Math.round(diffMin / 60);
  if (Math.abs(diffH) < 24) {
    return `${diffH}h`;
  }
  const diffD = Math.round(diffH / 24);
  return `${diffD}d`;
}


export function formatCount(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return "0";
  }
  return integerNumber.format(value);
}


export function formatCompact(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return "0";
  }
  return shortNumber.format(value);
}


export function formatDecimal(value: number | null | undefined, suffix = "") {
  if (value === null || value === undefined) {
    return "n/a";
  }
  return `${decimalNumber.format(value)}${suffix}`;
}


export function formatPct(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return "n/a";
  }
  return `${decimalNumber.format(value)}%`;
}


export function formatSignedPct(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return "n/a";
  }
  const sign = value > 0 ? "+" : "";
  return `${sign}${decimalNumber.format(value)}%`;
}


export function formatUsd(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return "n/a";
  }
  const sign = value < 0 ? "-" : "";
  return `${sign}$${moneyNumber.format(Math.abs(value))}`;
}


export function humanizeKey(value: string) {
  return value
    .replace(/[_/]+/g, " ")
    .replace(/\bjsonl\b/gi, "JSONL")
    .replace(/\bml\b/gi, "ML")
    .replace(/\bsqlite\b/gi, "SQLite")
    .replace(/\bapi\b/gi, "API")
    .replace(/\s+/g, " ")
    .trim();
}


export function shortenPath(value: string | null | undefined) {
  if (!value) {
    return "n/a";
  }
  const normalized = value.replaceAll("\\", "/");
  const chunks = normalized.split("/");
  return chunks.slice(Math.max(chunks.length - 3, 0)).join("/");
}
