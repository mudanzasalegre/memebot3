export type SourceState = "ok" | "empty" | "stale" | "missing" | "error";

export interface SourceStatus {
  source_key: string;
  kind: string;
  status: SourceState;
  updated_at: string | null;
  detail: string | null;
  path: string | null;
}

export interface ApiMeta {
  generated_at: string;
  degraded: boolean;
  empty: boolean;
  stale: boolean;
  source_status: SourceStatus[];
}

export interface Envelope<T> {
  data: T;
  meta: ApiMeta;
}

export interface HealthData {
  service: string;
  status: string;
  version: string;
}

export interface OverviewData {
  bot: {
    bot_id: string;
    process_state: string | null;
    dry_run: boolean | null;
    heartbeat_at: string | null;
    staleness: string | null;
    orchestration_status: BotProcessStatus | null;
    ui_managed: boolean | null;
    ui_can_start: boolean | null;
    ui_can_stop: boolean | null;
  };
  runtime: {
    discovery_paused: boolean | null;
    buys_paused: boolean | null;
    retrain_state: string | null;
    reports_refresh_state: string | null;
  };
  queue: {
    pending: number | null;
    requeued: number | null;
    cooldown: number | null;
    oldest_first_seen_at: string | null;
  };
  wallet: {
    wallet_sol: number | null;
    wallet_checked_at: string | null;
  };
  positions: {
    open_rows: number | null;
    closed_rows: number | null;
    win_rate_pct: number | null;
    avg_pnl_pct: number | null;
  };
  ml: {
    model_loaded: boolean | null;
    activation_ready: boolean | null;
    threshold: number | null;
  };
  research: {
    open_shadow_count: number | null;
    scorecard_generated_at: string | null;
  };
}

export interface SourcesStatusData {
  sources: SourceStatus[];
}

export interface RuntimeStats {
  raw_discovered: number | null;
  filtered_out: number | null;
  ai_pass: number | null;
  bought: number | null;
  sold: number | null;
  requeues: number | null;
  requeue_success: number | null;
  queue_added_total: number | null;
  pending_ai_vectors: number | null;
  open_shadow_positions: number | null;
  last_buy_at: string | null;
  last_sell_at: string | null;
}

export interface MlGateData {
  mode: string | null;
  enforced: boolean | null;
  threshold: number | null;
  activation_ready: boolean | null;
  dataset_quality_passed: boolean | null;
  model_loaded: boolean | null;
  model_exists: boolean | null;
  meta_exists: boolean | null;
  features_count: number | null;
  threshold_metric: string | null;
  rows: number | null;
  last_reload_at: string | null;
  last_decision_at: string | null;
}

export interface StrategyHealthEntry {
  requested_mode: string | null;
  health_state: string | null;
  trade_count: number | null;
  avg_pnl_pct: number | null;
  win_rate: number | null;
  exec_rate: number | null;
  price_rate: number | null;
  consecutive_losses: number | null;
  cooldown_until: string | null;
  disable_reason: string | null;
}

export interface ResearchRuntimeData {
  lane_enabled: boolean | null;
  shadow_enabled: boolean | null;
  open_shadow_count: number | null;
  scorecard_generated_at: string | null;
  thresholds_generated_at: string | null;
  last_event_at: string | null;
  open_shadow_by_regime: Record<string, number>;
}

export interface BuildInfoData {
  app: string | null;
  bot_version: string | null;
  git_sha: string | null;
  hostname: string | null;
  pid: number | null;
  python_version: string | null;
}

export interface RuntimeStateData {
  bot_id: string;
  updated_at: string | null;
  heartbeat_at: string | null;
  started_at: string | null;
  process_state: string | null;
  dry_run: boolean | null;
  discovery_paused: boolean | null;
  buys_paused: boolean | null;
  wallet_sol: number | null;
  wallet_checked_at: string | null;
  open_positions_count: number | null;
  queue_pending: number | null;
  queue_requeued: number | null;
  queue_cooldown: number | null;
  queue_oldest_first_seen_at: string | null;
  buy_limiter_in_window: number | null;
  buy_limiter_window_s: number | null;
  retrain_state: string | null;
  reports_refresh_state: string | null;
  discovery_last_ok_at: string | null;
  monitor_last_ok_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  stats: RuntimeStats;
  ml_gate: MlGateData;
  strategy_health: Record<string, StrategyHealthEntry>;
  research: ResearchRuntimeData;
  build_info: BuildInfoData;
}

export interface RuntimeStrategyHealthData {
  bot_id: string;
  updated_at: string | null;
  strategy_health: Record<string, StrategyHealthEntry>;
}

export interface RuntimeEventItem {
  id: string;
  ts_utc: string;
  event_type: string;
  address: string | null;
  summary: string;
  payload: Record<string, unknown>;
}

export interface RuntimeEventsData {
  items: RuntimeEventItem[];
  count: number;
  filters: {
    limit: number;
    before_ts: string | null;
    address: string | null;
    event_type: string | null;
  };
}

export type ResearchEventItem = RuntimeEventItem;
export type ResearchEventsData = RuntimeEventsData;

export interface DiscoveryFeedItem {
  id: string;
  stream: "runtime" | "research";
  event_type: string;
  ts_utc: string;
  address: string | null;
  symbol: string | null;
  regime: string | null;
  stage: string | null;
  action: string | null;
  reason: string | null;
  severity: "info" | "warning" | "success" | "danger";
  summary: string;
  payload: Record<string, unknown>;
}

export interface DiscoveryFeedData {
  items: DiscoveryFeedItem[];
  count: number;
  filters: {
    limit: number;
    before_ts: string | null;
    address: string | null;
    stage: string | null;
    decision_action: string | null;
    reason: string | null;
  };
}

export interface DiscoveryCounterRow {
  group: string;
  count: number;
}

export interface RequeueReasonRow {
  reason: string;
  events: number;
}

export interface DiscoverySummaryData {
  window_min: number;
  queue: {
    added: number;
    requeued: number;
    dropped: number;
    bought: number;
  };
  candidate_decisions: DiscoveryCounterRow[];
  candidate_stages: DiscoveryCounterRow[];
  requeue_reasons: RequeueReasonRow[];
}

export interface QueueSummaryData {
  captured_at: string | null;
  pending: number | null;
  requeued: number | null;
  cooldown: number | null;
  oldest_first_seen_at: string | null;
  recent_requeue_reasons: RequeueReasonRow[];
}

export interface QueueItem {
  status: string | null;
  address: string | null;
  symbol: string | null;
  discovered_via: string | null;
  entry_regime: string | null;
  dex_id: string | null;
  discovered_at: string | null;
  first_seen_at: string | null;
  queue_age_minutes: number | null;
  attempts: number | null;
  retries_left: number | null;
  next_retry_at: string | null;
  last_reason: string | null;
}

export interface QueueItemsData {
  captured_at: string | null;
  items: QueueItem[];
  count: number;
  filters: {
    status: string | null;
    limit: number;
    address: string | null;
  };
}

export interface LogsTailData {
  target: string;
  path: string;
  lines: string[];
  count: number;
}

export interface OpenPositionItem {
  trade_id: number;
  address: string | null;
  symbol: string | null;
  opened_at: string | null;
  qty: number | null;
  buy_price_usd: number | null;
  buy_amount_sol: number | null;
  entry_regime: string | null;
  size_bucket: string | null;
  size_multiplier: number | null;
  entry_ai_proba: number | null;
  entry_score_total: number | null;
  buy_liquidity_usd: number | null;
  buy_market_cap_usd: number | null;
  peak_price_usd: number | null;
  highest_pnl_pct: number | null;
}

export interface OpenPositionsData {
  items: OpenPositionItem[];
  count: number;
  filters: {
    address: string | null;
    limit: number;
  };
}

export interface ClosedTradeItem {
  trade_id: number;
  address: string | null;
  symbol: string | null;
  opened_at: string | null;
  closed_at: string | null;
  entry_regime: string | null;
  exit_reason: string | null;
  outcome: string | null;
  buy_amount_sol: number | null;
  size_bucket: string | null;
  size_multiplier: number | null;
  buy_price_usd: number | null;
  close_price_usd: number | null;
  effective_exit_price_usd: number | null;
  total_pnl_usd: number | null;
  total_pnl_pct: number | null;
  highest_pnl_pct: number | null;
  partial_taken: boolean;
  price_source_at_buy: string | null;
  price_source_at_close: string | null;
}

export interface LedgerConsistencyData {
  db_closed_rows: number | null;
  paper_closed_rows: number | null;
  scorecard_live_closed: number | null;
  scorecard_generated_at_utc: string | null;
  latest_closed_at: string | null;
  lag_rows: number | null;
  db_total_pnl_usd: number | null;
  paper_total_pnl_usd: number | null;
  paper_matches_db: boolean | null;
  pnl_matches_db: boolean | null;
  scorecard_stale_vs_latest_close: boolean | null;
  is_consistent: boolean;
}

export interface ClosedTradesSummary {
  closed_count: number;
  win_rate_pct: number | null;
  avg_pnl_pct: number | null;
  median_pnl_pct: number | null;
  total_pnl_usd: number | null;
  latest_closed_at: string | null;
}

export interface ClosedTradesData {
  items: ClosedTradeItem[];
  count: number;
  page_count: number;
  total_count: number;
  has_more: boolean;
  next_before_ts: string | null;
  next_before_id: number | null;
  filters: {
    limit: number;
    before_ts: string | null;
    before_id: number | null;
    outcome: string | null;
    exit_reason: string | null;
    entry_regime: string | null;
  };
  summary: ClosedTradesSummary;
  consistency: LedgerConsistencyData;
}

export interface TradeDetailTrade {
  trade_id: number;
  token_mint: string | null;
  address: string | null;
  symbol: string | null;
  qty: number | null;
  entry_qty: number | null;
  buy_price_usd: number | null;
  price_source_at_buy: string | null;
  buy_tx_sig: string | null;
  entry_regime: string | null;
  size_bucket: string | null;
  size_multiplier: number | null;
  buy_amount_sol: number | null;
  entry_notional_usd: number | null;
  entry_ai_proba: number | null;
  entry_score_total: number | null;
  buy_liquidity_usd: number | null;
  buy_market_cap_usd: number | null;
  buy_volume_24h_usd: number | null;
  peak_price_usd: number | null;
  peak_price: number | null;
  opened_at: string | null;
  closed: boolean | null;
  closed_at: string | null;
  close_price_usd: number | null;
  exit_tx_sig: string | null;
  price_source_at_close: string | null;
  exit_reason: string | null;
  outcome: string | null;
  highest_pnl_pct: number | null;
  realized_qty: number | null;
  realized_proceeds_usd: number | null;
  realized_cost_usd: number | null;
  realized_pnl_usd: number | null;
  effective_exit_price_usd: number | null;
  total_pnl_usd: number | null;
  total_pnl_pct: number | null;
  partial_taken: boolean | null;
  partial_count: number | null;
  first_partial_at: string | null;
  last_partial_at: string | null;
  last_partial_qty: number | null;
  last_partial_price_usd: number | null;
}

export interface TradeTokenData {
  address: string | null;
  symbol: string | null;
  name: string | null;
  created_at: string | null;
  liquidity_usd: number | null;
  volume_24h_usd: number | null;
  market_cap_usd: number | null;
  holders: number | null;
  rug_score: number | null;
  cluster_bad: boolean | null;
  social_ok: boolean | null;
  trend: string | null;
  insider_sig: boolean | null;
  score_total: number | null;
  dex_id: string | null;
  discovered_via: string | null;
  discovered_at: string | null;
}

export interface TradeComputedData {
  entry_qty: number | null;
  remaining_qty: number | null;
  realized_qty: number | null;
  realized_proceeds_usd: number | null;
  realized_cost_usd: number | null;
  realized_pnl_usd: number | null;
  unrealized_proceeds_usd: number | null;
  unrealized_cost_usd: number | null;
  unrealized_pnl_usd: number | null;
  total_proceeds_usd: number | null;
  total_cost_usd: number | null;
  total_pnl_usd: number | null;
  total_pnl_pct: number | null;
  effective_exit_price_usd: number | null;
  hold_minutes: number | null;
  outcome: string | null;
}

export interface TradeExecutionData {
  buy_tx_sig: string | null;
  exit_tx_sig: string | null;
  price_source_at_buy: string | null;
  price_source_at_close: string | null;
  partial_taken: boolean | null;
  partial_count: number | null;
  first_partial_at: string | null;
  last_partial_at: string | null;
  last_partial_qty: number | null;
  last_partial_price_usd: number | null;
}

export interface TradeDetailData {
  trade: TradeDetailTrade;
  token: TradeTokenData;
  computed: TradeComputedData;
  execution: TradeExecutionData;
}

export interface TradeReplayDerivedData {
  first_seen_at: string | null;
  minutes_first_seen_to_buy: number | null;
  hold_minutes: number | null;
}

export interface TradeReplayData {
  trade: TradeDetailTrade;
  token: TradeTokenData;
  entry_snapshot: Record<string, unknown> | null;
  runtime_timeline: RuntimeEventItem[];
  research_timeline: RuntimeEventItem[];
  derived: TradeReplayDerivedData;
}

export interface AnalyticsGroupRow {
  group: string;
  count: number;
  win_rate_pct: number | null;
  avg_pnl_pct: number | null;
  median_pnl_pct: number | null;
  sum_pnl_pct_points: number | null;
  avg_giveback_pct: number | null;
  avg_hold_minutes: number | null;
}

export interface AnalyticsCoverageRow {
  field: string;
  present_count: number;
  present_pct: number | null;
}

export interface AnalyticsRequeueReasonRow {
  reason: string;
  events: number;
  unique_addresses: number;
  bought_after_requeue: number;
  conversion_pct: number | null;
  avg_backoff_s: number | null;
}

export interface AnalyticsBaselineData {
  project_root: string | null;
  config: Record<string, unknown>;
  consistency: LedgerConsistencyData;
  positions: {
    rows: number;
    closed_rows: number;
    open_rows: number;
    win_rate_pct: number | null;
    avg_pnl_pct: number | null;
    median_pnl_pct: number | null;
    avg_hold_minutes: number | null;
    avg_giveback_pct: number | null;
    simple_max_drawdown_pct_points: number | null;
    exit_breakdown: Array<{
      exit_reason: string;
      count: number;
      avg_pnl_pct: number | null;
      median_pnl_pct: number | null;
      avg_giveback_pct: number | null;
    }>;
    partial_breakdown: Array<{
      partial_taken: boolean;
      count: number;
      avg_pnl_pct: number | null;
      median_pnl_pct: number | null;
    }>;
  };
  features: {
    files: string[];
    rows: number;
    positives: number;
    unique_tokens: number;
    constant_columns: string[];
    null_pct: Record<string, number>;
  };
}

export interface AnalyticsEdgeData {
  project_root: string | null;
  consistency: LedgerConsistencyData;
  overview: {
    closed_trades: number;
    win_rate_pct: number | null;
    avg_pnl_pct: number | null;
    median_pnl_pct: number | null;
    avg_giveback_pct: number | null;
  };
  exit_reason: AnalyticsGroupRow[];
  price_sources_buy: AnalyticsGroupRow[];
  price_sources_close: AnalyticsGroupRow[];
  price_source_pairs: AnalyticsGroupRow[];
  regimes: Record<string, AnalyticsGroupRow[]>;
  sizing: Record<string, AnalyticsGroupRow[]>;
  coverage: AnalyticsCoverageRow[];
  winners: {
    count: number;
    avg_giveback_pct: number | null;
    median_giveback_pct: number | null;
    giveback_ge_20pct_count: number;
    giveback_ge_40pct_count: number;
  };
  partials: {
    rows: AnalyticsGroupRow[];
    partial_taken_count: number;
    partial_winner_then_red_count: number;
    partial_winner_then_red_pct: number | null;
  };
  requeues: {
    events_path: string | null;
    rows: number;
    requeue_rows: AnalyticsRequeueReasonRow[];
    addresses_requeued: number;
    addresses_bought_after_requeue: number;
    avg_minutes_first_seen_to_buy: number | null;
    avg_requeues_before_buy: number | null;
  };
}

export interface MlRuntimeData {
  model_exists: boolean | null;
  meta_exists: boolean | null;
  model_loaded: boolean | null;
  features_count: number | null;
  activation_ready: boolean | null;
  dataset_quality_passed: boolean | null;
  threshold_metric: string | null;
  rows: number | null;
  model_path: string | null;
  meta_path: string | null;
}

export interface MlGateStatusData {
  mode: string | null;
  enforced: boolean | null;
  threshold: number | null;
  activation_ready: boolean | null;
}

export interface MlStatusData {
  runtime: MlRuntimeData;
  gate: MlGateStatusData;
  train_status: Record<string, unknown> | null;
  recommended_threshold: Record<string, unknown> | null;
  dataset_quality: Record<string, unknown> | null;
}

export interface MlResearchData {
  scorecard: Record<string, unknown> | null;
  thresholds: Record<string, unknown> | null;
  post_partial_experiment: Record<string, unknown> | null;
  research_events: {
    rows: number;
    last_event_at: string | null;
  };
  consistency: LedgerConsistencyData;
}

export interface ConfigPoliciesData {
  filters: Record<string, unknown>;
  sizing: Record<string, unknown>;
  exit: Record<string, unknown>;
  strategy: Record<string, unknown>;
}

export type ConfigEffectiveData = Record<string, unknown>;
export type AuthMode = "local" | "dev";

export interface AuthUser {
  username: string;
  display_name: string;
  role: "viewer" | "operator" | "admin";
  permissions: string[];
  auth_mode: AuthMode;
  is_dev_mode: boolean;
  expires_at: string | null;
}

export interface AuthAvailableUser {
  username: string;
  display_name: string;
  role: "viewer" | "operator" | "admin";
}

export interface AuthSessionData {
  auth_mode: AuthMode;
  is_authenticated: boolean;
  user: AuthUser | null;
  available_users: AuthAvailableUser[];
  default_credentials_active: boolean;
  dev_mode: boolean;
  loopback_only: boolean;
}
export type ControlCommandType =
  | "pause_discovery"
  | "resume_discovery"
  | "pause_buys"
  | "resume_buys"
  | "reload_model"
  | "trigger_retrain"
  | "refresh_reports"
  | "set_log_level";

export type ControlCommandStatus =
  | "pending"
  | "running"
  | "done"
  | "failed"
  | "rejected"
  | "cancelled";

export interface ControlCommandItem {
  id: number;
  bot_id: string;
  command_type: ControlCommandType;
  status: ControlCommandStatus;
  requested_by: string | null;
  requested_from: string | null;
  idempotency_key: string | null;
  requested_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  error_text: string | null;
}

export type BotProcessStatus =
  | "stopped"
  | "starting"
  | "running_managed"
  | "running_external"
  | "crashed";

export interface BotProcessData {
  bot_id: string;
  status: BotProcessStatus;
  detail: string;
  managed: boolean;
  external: boolean;
  can_start: boolean;
  can_stop: boolean;
  pid: number | null;
  managed_pid: number | null;
  runtime_pid: number | null;
  runtime_freshness: string | null;
  runtime_heartbeat_at: string | null;
  runtime_updated_at: string | null;
  runtime_process_state: string | null;
  state_file_path: string;
  console_log_path: string | null;
  started_at: string | null;
  started_by: string | null;
  requested_from: string | null;
  dry_run: boolean | null;
  file_log: boolean | null;
  command: string[];
  startup_grace_s: number | null;
  last_stopped_by?: string | null;
}

export interface ControlStateData {
  bot_id: string;
  runtime: {
    updated_at: string | null;
    heartbeat_at: string | null;
    process_state: string | null;
    discovery_paused: boolean | null;
    buys_paused: boolean | null;
    retrain_state: string | null;
    reports_refresh_state: string | null;
    last_error: string | null;
    staleness: string | null;
    heartbeat_age_s: number | null;
  };
  process: BotProcessData;
  commands: {
    counts_by_status: Record<string, number>;
    pending_count: number;
    running_count: number;
    last_command: ControlCommandItem | null;
  };
}

export interface ControlCommandsData {
  items: ControlCommandItem[];
  limit: number;
  before_ts: string | null;
  status: string | null;
  command_type: string | null;
}

export interface ControlCommandCreateRequest {
  bot_id: string;
  command_type: ControlCommandType;
  payload: Record<string, unknown>;
  requested_by?: string | null;
  requested_from?: string | null;
  idempotency_key?: string | null;
}

export interface ControlCommandCreateData {
  id: number;
  status: ControlCommandStatus;
}

export interface BotProcessStartRequest {
  bot_id: string;
  dry_run: boolean;
  file_log: boolean;
  requested_from?: string | null;
}

export interface BotProcessStopRequest {
  bot_id: string;
  force: boolean;
}

export interface SavedViewItem {
  id: number;
  page_key: string;
  view_name: string;
  filters: Record<string, unknown>;
  layout: Record<string, unknown>;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
  can_edit: boolean;
  can_delete: boolean;
}

export interface SavedViewsData {
  items: SavedViewItem[];
  page_key: string | null;
  mine: boolean;
}

export interface SavedViewCreateRequest {
  page_key: string;
  view_name: string;
  filters: Record<string, unknown>;
  layout: Record<string, unknown>;
}

export interface SavedViewUpdateRequest {
  view_name?: string;
  filters?: Record<string, unknown>;
  layout?: Record<string, unknown>;
}

export interface SavedViewDeleteData {
  id: number;
  deleted: boolean;
}

const apiBaseUrl = ((import.meta.env.VITE_API_BASE_URL as string | undefined) || "").replace(/\/$/, "");

async function parseErrorMessage(response: Response, path: string) {
  let detail = `HTTP ${response.status} while requesting ${path}`;
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload?.detail === "string" && payload.detail.trim()) {
      detail = payload.detail;
    }
  } catch {
    // Ignore non-JSON error responses and keep the generic message.
  }
  return detail;
}


async function requestEnvelope<T>(
  path: string,
  init: RequestInit & { signal?: AbortSignal },
): Promise<Envelope<T>> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    credentials: "include",
    ...init,
  });

  if (!response.ok) {
    throw new Error(await parseErrorMessage(response, path));
  }

  return (await response.json()) as Envelope<T>;
}


export async function fetchEnvelope<T>(path: string, signal?: AbortSignal): Promise<Envelope<T>> {
  return requestEnvelope<T>(path, {
    headers: {
      Accept: "application/json",
    },
    signal,
  });
}


export async function postEnvelope<TResponse, TRequest>(
  path: string,
  body: TRequest,
  options?: {
    signal?: AbortSignal;
    headers?: Record<string, string>;
  },
): Promise<Envelope<TResponse>> {
  return requestEnvelope<TResponse>(path, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    },
    body: JSON.stringify(body),
    signal: options?.signal,
  });
}


export async function patchEnvelope<TResponse, TRequest>(
  path: string,
  body: TRequest,
  options?: {
    signal?: AbortSignal;
    headers?: Record<string, string>;
  },
): Promise<Envelope<TResponse>> {
  return requestEnvelope<TResponse>(path, {
    method: "PATCH",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    },
    body: JSON.stringify(body),
    signal: options?.signal,
  });
}


export async function deleteEnvelope<TResponse>(
  path: string,
  options?: {
    signal?: AbortSignal;
    headers?: Record<string, string>;
  },
): Promise<Envelope<TResponse>> {
  return requestEnvelope<TResponse>(path, {
    method: "DELETE",
    headers: {
      Accept: "application/json",
      ...(options?.headers || {}),
    },
    signal: options?.signal,
  });
}
