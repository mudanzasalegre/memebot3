export interface EndpointDescriptor {
  path: string;
  purpose: string;
  cadence: string;
}

export interface RouteDescriptor {
  id: string;
  path: string;
  navLabel: string;
  title: string;
  eyebrow: string;
  group: "Monitor" | "Inspect" | "Operate";
  phase: string;
  summary: string;
  question: string;
  accent: "mineral" | "teal" | "olive" | "amber";
  inNav?: boolean;
  panels: string[];
  endpoints: EndpointDescriptor[];
}


export const routeCatalog: RouteDescriptor[] = [
  {
    id: "overview",
    path: "/overview",
    navLabel: "Overview",
    title: "Overview",
    eyebrow: "Monitor / daily cockpit",
    group: "Monitor",
    phase: "PR-UI-9",
    summary: "Front page for runtime posture, queue pressure, wallet state, ML readiness, and source truth.",
    question: "Is the system alive, coherent, and worth watching right now?",
    accent: "teal",
    panels: ["status hero", "queue / wallet / ML summary", "source health strip", "positions snapshot"],
    endpoints: [
      { path: "/api/v1/overview", purpose: "Operational front page", cadence: "5s" },
      { path: "/api/v1/sources/status", purpose: "Source truth health", cadence: "15s" },
    ],
  },
  {
    id: "runtime",
    path: "/runtime",
    navLabel: "Runtime",
    title: "Runtime",
    eyebrow: "Monitor / live pulse",
    group: "Monitor",
    phase: "PR-UI-9",
    summary: "Heartbeat, pause flags, runtime events, strategy health, and buy limiter telemetry.",
    question: "Which part of the runtime is failing, paused, or backing off?",
    accent: "olive",
    panels: ["runtime pulse", "strategy health strip", "last error rail", "buy limiter state"],
    endpoints: [
      { path: "/api/v1/runtime/state", purpose: "Live runtime snapshot", cadence: "3s" },
      { path: "/api/v1/runtime/strategy-health", purpose: "Regime health breakdown", cadence: "3s" },
      { path: "/api/v1/runtime/events", purpose: "Normalized event timeline", cadence: "2s" },
    ],
  },
  {
    id: "sniper",
    path: "/sniper",
    navLabel: "Sniper",
    title: "Sniper",
    eyebrow: "Monitor / green sniper",
    group: "Monitor",
    phase: "PR-UI-SNIPER",
    summary: "Hot queue, green-sniper policy, missed pumps, reject reasons, and live-canary posture.",
    question: "Is the sniper seeing hot newborn pumps quickly enough, and why is it missing any of them?",
    accent: "amber",
    panels: ["hot queue", "green sniper policy", "reject reasons", "missed pumps"],
    endpoints: [
      { path: "/api/v1/sniper/status", purpose: "Sniper lane status and policy", cadence: "5s" },
      { path: "/api/v1/sniper/missed-pumps", purpose: "Missed pump opportunities", cadence: "30s" },
      { path: "/api/v1/sniper/hot-queue", purpose: "Hot queue snapshot", cadence: "5s" },
    ],
  },
  {
    id: "discovery",
    path: "/discovery",
    navLabel: "Discovery",
    title: "Discovery",
    eyebrow: "Monitor / funnel",
    group: "Monitor",
    phase: "PR-UI-10",
    summary: "Unified feed of reject, wait, shadow, and buy decisions before execution.",
    question: "Where is the discovery funnel collapsing before a buy happens?",
    accent: "amber",
    panels: ["funnel timeline", "reason breakdown", "stage filters", "requeue pressure"],
    endpoints: [
      { path: "/api/v1/discovery/feed", purpose: "Unified funnel feed", cadence: "5s" },
      { path: "/api/v1/discovery/summary", purpose: "Stage and reason aggregates", cadence: "5s" },
    ],
  },
  {
    id: "queue",
    path: "/queue",
    navLabel: "Queue",
    title: "Queue",
    eyebrow: "Monitor / backlog",
    group: "Monitor",
    phase: "PR-UI-10",
    summary: "Live queue inspection with retries, next retry, and oldest candidate visibility.",
    question: "What is waiting in the queue, and why is it still there?",
    accent: "teal",
    panels: ["queue summary", "live items table", "retry clock", "oldest candidate"],
    endpoints: [
      { path: "/api/v1/queue/summary", purpose: "Queue counters", cadence: "3s" },
      { path: "/api/v1/queue/items", purpose: "Current queue items", cadence: "3s" },
    ],
  },
  {
    id: "positions",
    path: "/positions",
    navLabel: "Positions",
    title: "Positions",
    eyebrow: "Inspect / open risk",
    group: "Inspect",
    phase: "PR-UI-11",
    summary: "Open position inventory focused on current risk, unrealized posture, and trade replay entry points.",
    question: "How much open risk is live right now?",
    accent: "olive",
    panels: ["open positions table", "PnL strip", "regime exposure", "replay shortcuts"],
    endpoints: [
      { path: "/api/v1/positions/open", purpose: "Open positions", cadence: "5s" },
    ],
  },
  {
    id: "trades",
    path: "/trades",
    navLabel: "Trades",
    title: "Trades",
    eyebrow: "Inspect / closed loop",
    group: "Inspect",
    phase: "PR-UI-11",
    summary: "Closed trade history with filters, outcome posture, and replay jump-off points.",
    question: "How has the system been closing, and where is edge actually landing?",
    accent: "mineral",
    panels: ["closed trades table", "outcome filters", "win / loss posture", "replay access"],
    endpoints: [
      { path: "/api/v1/trades/closed", purpose: "Closed trade ledger", cadence: "5s" },
    ],
  },
  {
    id: "tradeReplay",
    path: "/trades/:tradeId",
    navLabel: "Trade Replay",
    title: "Trade Replay",
    eyebrow: "Inspect / reconstruction",
    group: "Inspect",
    phase: "PR-UI-11",
    summary: "End-to-end reconstruction of a trade from T0, runtime timeline, research notes, and final exit.",
    question: "What happened exactly between first sight and final exit for this trade?",
    accent: "amber",
    inNav: false,
    panels: ["trade factsheet", "T0 parquet snapshot", "runtime rail", "research rail"],
    endpoints: [
      { path: "/api/v1/trades/{trade_id}", purpose: "Trade details", cadence: "5s" },
      { path: "/api/v1/trades/{trade_id}/replay", purpose: "Trade replay bundle", cadence: "5s" },
    ],
  },
  {
    id: "analytics",
    path: "/analytics",
    navLabel: "Analytics",
    title: "Analytics",
    eyebrow: "Inspect / edge",
    group: "Inspect",
    phase: "PR-UI-12",
    summary: "Edge distribution by exit, regime, sizing, and feature coverage.",
    question: "Where is edge concentrated, and where is it leaking away?",
    accent: "teal",
    panels: ["edge overview", "exit buckets", "regime spread", "feature coverage"],
    endpoints: [
      { path: "/api/v1/analytics/edge", purpose: "Edge summary", cadence: "60s" },
      { path: "/api/v1/analytics/baseline", purpose: "Project baseline", cadence: "60s" },
    ],
  },
  {
    id: "ml",
    path: "/ml",
    navLabel: "ML Center",
    title: "ML Center",
    eyebrow: "Inspect / model",
    group: "Inspect",
    phase: "PR-UI-12",
    summary: "Model runtime, gate state, threshold posture, and research scorecard visibility.",
    question: "Is the model usable, coherent, and safe to trust in runtime?",
    accent: "olive",
    panels: ["model state", "threshold posture", "dataset quality", "research scorecard"],
    endpoints: [
      { path: "/api/v1/ml/status", purpose: "ML runtime state", cadence: "15s" },
      { path: "/api/v1/ml/research", purpose: "Research scorecard and thresholds", cadence: "15s" },
    ],
  },
  {
    id: "policy",
    path: "/policy",
    navLabel: "Policy Center",
    title: "Policy Center",
    eyebrow: "Inspect / learned policy",
    group: "Inspect",
    phase: "PR-UI-POLICY",
    summary: "Read-only control plane for PR-00..PR-36 safety gates, replay, ledger, funnel attribution, proposals, model families, and rollout posture.",
    question: "What proof exists before any learned policy can affect paper or live canary?",
    accent: "amber",
    panels: ["safety gates", "policy replay", "decision ledger", "funnel attribution", "proposals"],
    endpoints: [
      { path: "/api/v1/policy/safety", purpose: "Learned-policy safety gate bundle", cadence: "15s" },
      { path: "/api/v1/policy/replay", purpose: "Policy replay comparison", cadence: "60s" },
      { path: "/api/v1/policy/decision-ledger", purpose: "Canonical entry decision ledger", cadence: "15s" },
      { path: "/api/v1/policy/funnel-attribution", purpose: "Token-level funnel attribution", cadence: "30s" },
      { path: "/api/v1/policy/proposals", purpose: "Candidate strategy proposals", cadence: "60s" },
    ],
  },
  {
    id: "research",
    path: "/research",
    navLabel: "AutoResearch",
    title: "AutoResearch",
    eyebrow: "Inspect / autonomous research",
    group: "Inspect",
    phase: "PR-AR-31/32",
    summary: "Read-only AutoResearch panel for scoreboard, current best policy, replay runs, paper-forward status, API budget, and moonshot progress.",
    question: "Which candidate is improving the objective, and where is the research loop currently blocked?",
    accent: "olive",
    panels: ["scoreboard", "current best policy", "paper forward", "api budget", "moonshot progress"],
    endpoints: [
      { path: "/api/v1/research/scoreboard", purpose: "AutoResearch scoreboard", cadence: "15s" },
      { path: "/api/v1/research/runs", purpose: "Replay run snapshots", cadence: "15s" },
      { path: "/api/v1/research/current-best", purpose: "Current best candidate policy", cadence: "15s" },
      { path: "/api/v1/research/api-budget", purpose: "API budget posture", cadence: "30s" },
      { path: "/api/v1/research/moonshot-progress", purpose: "Moonshot and runner capture progress", cadence: "30s" },
      { path: "/api/v1/research/paper-forward", purpose: "Paper-forward validation status", cadence: "15s" },
    ],
  },
  {
    id: "config",
    path: "/config",
    navLabel: "Config Center",
    title: "Config Center",
    eyebrow: "Inspect / policy",
    group: "Inspect",
    phase: "PR-UI-12",
    summary: "Effective runtime policy across filters, sizing, exits, and strategy regimes.",
    question: "Which policy is actually running right now, not just documented?",
    accent: "mineral",
    panels: ["effective config", "filter policy", "sizing policy", "regime strategy"],
    endpoints: [
      { path: "/api/v1/config/effective", purpose: "Effective config", cadence: "60s" },
      { path: "/api/v1/config/policies", purpose: "Derived policies", cadence: "60s" },
    ],
  },
  {
    id: "logs",
    path: "/logs",
    navLabel: "Logs and Events",
    title: "Logs and Events",
    eyebrow: "Inspect / traces",
    group: "Inspect",
    phase: "PR-UI-10",
    summary: "Operational tail for app logs, runtime events, and research events.",
    question: "What happened most recently, in exact chronological terms?",
    accent: "amber",
    panels: ["tail stream", "runtime rail", "research rail", "target switcher"],
    endpoints: [
      { path: "/api/v1/logs/tail", purpose: "Whitelisted log tail", cadence: "2s" },
      { path: "/api/v1/events/runtime", purpose: "Runtime event feed", cadence: "2s" },
      { path: "/api/v1/events/research", purpose: "Research event feed", cadence: "2s" },
    ],
  },
  {
    id: "control",
    path: "/control",
    navLabel: "Control Center",
    title: "Control Center",
    eyebrow: "Operate / command bus",
    group: "Operate",
    phase: "PR-UI-15",
    summary: "Command center for pause, resume, retrain, refresh, audit trail, and role-gated local operations.",
    question: "What action should the operator take, and how will it be audited?",
    accent: "teal",
    panels: ["quick commands", "audit feed", "command status", "safety confirmations", "saved views"],
    endpoints: [
      { path: "/api/v1/auth/session", purpose: "Local session and role", cadence: "on load" },
      { path: "/api/v1/control/state", purpose: "Control posture", cadence: "3s" },
      { path: "/api/v1/control/commands", purpose: "Command history", cadence: "3s" },
      { path: "/api/v1/control/commands", purpose: "Command submission", cadence: "on demand" },
      { path: "/api/v1/saved-views", purpose: "Local saved view presets", cadence: "15s / on demand" },
    ],
  },
];


export const navGroups: Array<RouteDescriptor["group"]> = ["Monitor", "Inspect", "Operate"];


export function getRouteById(id: string) {
  return routeCatalog.find((route) => route.id === id);
}
