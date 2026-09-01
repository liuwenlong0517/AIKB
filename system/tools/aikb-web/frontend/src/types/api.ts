/** Web API 的共同响应包络。 */
export interface ApiMeta {
  request_id?: string;
  api_version?: string;
  degraded?: boolean;
  warnings?: string[];
  [key: string]: unknown;
}

export interface ApiResponse<T> { data: T; meta?: ApiMeta }
export interface ApiErrorBody { error: { code?: string; message: string; details?: unknown }; meta?: ApiMeta }

export interface CoreDocumentSummary {
  id: string;
  title: string;
  type: string;
  status: 'verified';
  path: string;
  tags?: string[];
  summary?: string;
  last_verified?: string | null;
  content_hash?: string;
  section?: string;
  excerpt?: string;
  matched_by?: string;
}

export interface OverviewData {
  document_count: number;
  by_type: Record<string, number>;
  by_tag: Array<{ tag: string; count: number }>;
  recent_documents: CoreDocumentSummary[];
  index?: { tokenizer?: string; rebuilt?: boolean };
}

/** 控制仓人类维护手册的只读投影；manual_id 仅允许服务端固定注册值。 */
export interface ManualData {
  manual_id: 'project' | 'commands' | string;
  title: string;
  content: string;
  content_hash: string;
  revision: string;
  truncated?: boolean;
}

export interface TreeNode {
  name: string;
  path: string;
  kind: 'directory' | 'document';
  id?: string;
  title?: string;
  type?: string;
  status?: 'verified';
  children?: TreeNode[];
}

export interface SearchData {
  query: string;
  count: number;
  results: CoreDocumentSummary[];
  index?: { tokenizer?: string; rebuilt?: boolean };
}

export interface KnowledgeRelation { direction: string; type: string; target: string; target_title?: string | null }

export interface DocumentData extends CoreDocumentSummary {
  content: string;
  applicable_versions?: string;
  truncated?: boolean;
  relations?: KnowledgeRelation[];
}

export interface RepositoryState { available: boolean; branch?: string | null; short_commit?: string | null }

export interface SystemInfoData {
  platform: { name: string; architecture: string };
  python: { version: string };
  repositories: { control: RepositoryState; knowledge: RepositoryState };
  index: { available?: boolean; tokenizer?: string; rebuilt?: boolean };
  /** 规则事务协调器的安全恢复摘要；不存在时表示部署未提供该能力。 */
  rule_writes?: RuleWriteStatus;
}

export interface RuleWriteStatus {
  available?: boolean;
  blocked?: boolean;
  recovery_required?: boolean;
  warning?: string;
}

export interface CapabilityData {
  platform: { platform: string; supported: boolean; reason?: string };
  read_only: boolean;
  capabilities: Array<{ id: string; supported: boolean; reason?: string }>;
}

export interface SystemData { info: SystemInfoData; capabilities: CapabilityData }
export interface SearchFilters { type?: string; tag?: string }

/** 运行状态只读投影中的稳定状态枚举；关闭状态不会出现在阶段 2 活动列表。 */
export type WorkingStateStatus = 'planned' | 'active' | 'blocked';
/** 历史 Working State 的终态；活动接口不会接受这些值。 */
export type ArchivedWorkingStateStatus = 'completed' | 'abandoned' | 'superseded';
export type RuntimeWorkingStateStatus = WorkingStateStatus | ArchivedWorkingStateStatus;

export interface Pagination {
  page: number;
  page_size: number;
  total: number | null;
  has_next: boolean;
  has_previous?: boolean;
  total_pages?: number;
}

export interface RuntimeRepositorySummary {
  role: string;
  available?: boolean;
  branch?: string | null;
  revision?: string | null;
  dirty?: boolean;
}

export interface WorkingStateSummary {
  work_id: string;
  project_id?: string | null;
  status: RuntimeWorkingStateStatus;
  work_schema_version?: string | null;
  /** 兼容字段：始终表示最新检查点作者，不表示 owner。 */
  agent?: string | null;
  session_id?: string | null;
  role?: string | null;
  author_agent?: string | null;
  author_session_id?: string | null;
  author_role?: string | null;
  owner_agent?: string | null;
  owner_session_id?: string | null;
  ownership_mode?: 'session-bound' | 'shared' | 'handed-off' | 'legacy-unbound' | string | null;
  ownership_binding?: string | null;
  participants?: WorkStateParticipant[];
  participant_count?: number;
  updated_at?: string | null;
  checkpoint_id?: string | null;
  goal?: string | null;
  current_state?: string | null;
  next_steps?: string | string[] | null;
  blockers?: string | null;
  branch?: string | null;
  base_revision?: string | null;
  workspace_dirty?: boolean | null;
  repositories?: RuntimeRepositorySummary[];
}

export interface WorkingStateDetail extends WorkingStateSummary {
  sections?: Record<string, string | string[] | null>;
  detail_status?: string | null;
  sensitivity?: string | null;
  checkpoint_count?: number;
  latest_checkpoint?: string | null;
  resume_capsule?: string | null;
}

export interface CheckpointSummary {
  checkpoint_id: string;
  based_on?: string | null;
  status?: WorkingStateStatus | string | null;
  work_schema_version?: string | null;
  /** 检查点的事实作者；不能称为 owner。 */
  agent?: string | null;
  session_id?: string | null;
  role?: string | null;
  author_agent?: string | null;
  author_session_id?: string | null;
  author_role?: string | null;
  updated_at?: string | null;
  workspace_dirty?: boolean | null;
  repositories?: RuntimeRepositorySummary[];
  truncated?: boolean;
  detail_status?: string | null;
}

export interface WorkStateParticipant {
  agent: string;
  session_id: string;
  role?: string | null;
}

export interface CheckpointDetail extends CheckpointSummary {
  sections?: Record<string, string | string[] | null>;
  goal?: string | null;
  current_state?: string | null;
  next_steps?: string | string[] | null;
  blockers?: string | null;
  verification?: string | string[] | null;
  changed_files?: string[] | null;
}

export interface RuntimeListData {
  items: WorkingStateSummary[];
  pagination: Pagination;
  count?: number;
  index?: { status?: string; rebuilt?: boolean; available?: boolean };
}

/** 历史运行状态列表与活动列表共享分页形状，但通过独立 API 保持生命周期边界。 */
export type ArchivedRuntimeListData = RuntimeListData;

export interface CheckpointListData {
  items: CheckpointSummary[];
  pagination: Pagination;
  count?: number;
  index?: { status?: string; rebuilt?: boolean; available?: boolean };
}

export type AuditStatus = 'started' | 'succeeded' | 'failed' | 'noop' | 'blocked' | 'incomplete' | 'cancelled' | 'timed_out' | 'interrupted';
export type AuditSource = 'mcp' | 'hook' | 'web';

export interface AuditSummaryData {
  count: number;
  statuses: Partial<Record<AuditStatus, number>>;
  agents: Record<string, number>;
  sources: Partial<Record<AuditSource, number>>;
  operations: Record<string, number>;
  average_duration_ms?: number | null;
  fallback_records?: number;
  damaged_count?: number;
  last_activity?: string | null;
}

export interface AuditEvent {
  invocation_id?: string | null;
  event_id?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  source?: AuditSource | string | null;
  agent?: string | null;
  session_label?: string | null;
  session_id?: string | null;
  project_id?: string | null;
  operation?: string | null;
  action_text?: string | null;
  status: AuditStatus;
  outcome_code?: string | null;
  result_text?: string | null;
  capture_level?: 'safe' | 'diagnostic' | 'full-local' | string | null;
  duration_ms?: number | null;
  error_type?: string | null;
  fallback?: boolean;
}

export interface AuditListData {
  items: AuditEvent[];
  pagination: Pagination;
  summary?: { has_damaged?: boolean; damaged_count?: number };
}

/** 阶段 3 首批受控动作风险级别；前端不据此推断未公开能力。 */
export type ActionRiskLevel = 'read_only' | 'derived_write';

export interface ActionSpec {
  action_id: string;
  title: string;
  description: string;
  supported_platforms: string[];
  risk_level: ActionRiskLevel | string;
  effects: string[];
  executor_kind?: string;
  program_key?: string;
  timeout_seconds: number;
  concurrency_group?: string;
  concurrency_limit?: number;
  parameter_schema: Record<string, unknown>;
  confirmation_required?: boolean;
  supported?: boolean;
  reason?: string | null;
}

export interface ActionsData { items: ActionSpec[] }

export interface ActionPreview {
  action_id: string;
  parameters: Record<string, unknown>;
  steps: string[];
  risk_level: ActionRiskLevel | string;
  effects: string[];
  timeout_seconds: number;
  concurrency_group?: string;
  confirmation_required?: boolean;
  preview_digest: string;
}

export interface ActionPreviewData {
  preview: ActionPreview;
  confirmation_token: string;
  expires_in_seconds: number;
}

export type TaskStatus = 'queued' | 'running' | 'cancelling' | 'succeeded' | 'failed' | 'timed_out' | 'cancelled' | 'interrupted';
export type TaskEventType = 'snapshot' | 'status' | 'progress' | 'output' | 'result' | 'heartbeat';

export interface TaskSnapshot {
  task_id: string;
  action_id: string;
  parameters?: Record<string, unknown>;
  risk_level?: ActionRiskLevel | string;
  effects?: string[];
  timeout_seconds?: number;
  status: TaskStatus | string;
  created_at?: string | null;
  updated_at?: string | null;
  progress?: number | null;
  output?: string | null;
  output_truncated?: boolean;
  result?: unknown;
  invocation_id?: string | null;
  last_event_id?: number | null;
  last_reason?: string | null;
}

export interface TasksData { items: TaskSnapshot[]; total?: number | null }
export interface TaskData { task: TaskSnapshot }

export interface TaskEvent {
  event_id: number;
  type: TaskEventType | string;
  task_id?: string;
  status?: TaskStatus | string;
  progress?: number | null;
  text?: string | null;
  output?: string | null;
  result?: unknown;
  task?: TaskSnapshot;
  snapshot?: TaskSnapshot;
  replay_reset?: boolean;
  [key: string]: unknown;
}

/** 阶段 4A 静态规则目录项；路径永远不由浏览器接收或推导。 */
export interface RuleSummary {
  rule_id: 'entry' | 'user' | 'agent' | 'contributing' | string;
  title: string;
  description: string;
  readable: boolean;
  writable: boolean;
  risk_level: string;
  /** 超过该值仅提示；超过 max_chars 才会阻断候选。 */
  recommended_chars?: number;
  max_chars: number;
  content_hash?: string;
  revision?: string;
}

/** 规则详情正文和服务端安全元数据。 */
export interface RuleDetail extends RuleSummary {
  content: string;
  content_hash: string;
  revision: string;
}

/** 候选正文校验投影；正文和完整 diff 只在预览响应期间留在内存。 */
export interface RuleValidation {
  valid?: boolean;
  ok?: boolean;
  errors?: Array<string | { code?: string; message?: string }>;
  warnings?: Array<string | { code?: string; message?: string }>;
  [key: string]: unknown;
}

/** 规则预览结果；令牌只在当前页面内存中短暂保留，并仅用于后续受控应用。 */
export interface RulePreviewData {
  rule_id: string;
  change_id: string;
  before_hash?: string;
  after_hash?: string;
  diff_hash?: string;
  diff?: string;
  unified_diff?: string;
  validation?: RuleValidation;
  preview_digest: string;
  confirmation_token?: string;
  expires_at?: string;
  expires_in_seconds?: number;
  revision?: string;
  [key: string]: unknown;
}

export interface RulesData { items: RuleSummary[] }

/** 规则应用接口只返回逻辑变更/任务关联和安全状态，不包含正文、diff 或物理路径。 */
export type RuleChangeStatus = 'prepared' | 'applying' | 'validating' | 'succeeded' | 'expired' | 'rejected' | 'rolling_back' | 'rolled_back' | 'recovery_required' | string;

export interface RuleChangeData {
  change_id: string;
  rule_id?: string;
  action_id?: string;
  risk_level?: string;
  status: RuleChangeStatus;
  before_hash?: string;
  after_hash?: string;
  diff_hash?: string;
  preview_digest?: string;
  repository_revision?: string;
  task_id?: string | null;
  rollback_status?: 'not_applicable' | 'not_started' | 'pending' | 'succeeded' | 'recovery_required' | string;
  error_code?: string | null;
  error_message?: string | null;
  [key: string]: unknown;
}

/** GET /rules/changes/{change_id} 的完整安全包装，保留变更、任务关联与全局阻断标记。 */
export interface RuleChangeEnvelope {
  change: RuleChangeData;
  task?: { task_id?: string; status?: string; action_id?: string; change_id?: string; created_at?: string | null } | null;
  blocked?: boolean;
}

/** apply 响应中的任务/变更安全关联；不允许携带候选正文或路径。 */
export interface RuleApplyData {
  change_id: string;
  task_id?: string | null;
  status?: RuleChangeStatus;
  change?: RuleChangeData;
  task?: TaskSnapshot;
  [key: string]: unknown;
}

/** 阶段 4B 固定维护目标的公开摘要；只包含逻辑 ID 和安全状态。 */
export type MaintenanceTargetStatus = 'ready' | 'missing' | 'drifted' | 'conflict' | 'invalid' | 'unsupported' | 'restart_required';

export interface MaintenancePlatformData {
  platform: string;
  architecture?: string;
  supported: boolean;
  inspection_supported?: boolean;
  preview_supported?: boolean;
  apply_supported?: boolean;
  reason_code?: string | null;
}

/** 阶段 4B 只允许服务端声明的固定维护目标，避免未知目标进入页面分支。 */
export type MaintenanceTargetId = 'environment' | 'agent.codex' | 'agent.claude-code';

export interface MaintenanceLeafData {
  leaf_id: string;
  existence?: 'present' | 'missing' | string;
  progress?: string;
  before_hash?: string | null;
  expected_hash?: string;
}

export interface MaintenanceTargetSummary {
  target_id: MaintenanceTargetId;
  title: string;
  description: string;
  risk_level: string;
  action_id: string;
  status?: MaintenanceTargetStatus;
  reason_code?: string | null;
  logical_leaves?: string[];
  steps?: string[];
  base_fingerprint?: string | null;
  restart_required?: boolean;
  supported_platforms?: string[];
}

export interface MaintenanceTargetsData {
  items: MaintenanceTargetSummary[];
  platform: MaintenancePlatformData;
}

export interface MaintenanceTargetStateData {
  target_id: MaintenanceTargetId;
  status: MaintenanceTargetStatus;
  reason_code?: string | null;
  logical_leaves: string[];
  steps: string[];
  base_fingerprint?: string | null;
  restart_required?: boolean;
}

/** GET 目标详情的安全包络；状态与静态目标定义分开，便于服务端保持字段边界。 */
export interface MaintenanceTargetDetail {
  target: MaintenanceTargetSummary;
  platform: MaintenancePlatformData;
  status: MaintenanceTargetStateData;
  leaves?: MaintenanceLeafData[];
}

/** 只读结构化差异：页面只渲染服务端允许的语义字段，不回显配置正文或路径。 */
export interface MaintenanceDiffData {
  leaf_id: string;
  difference_code?: 'unchanged' | 'missing' | 'drifted' | 'conflict' | 'invalid';
  /** 服务端固定白名单提供的人类可读语义，不是配置正文或用户输入。 */
  display_name?: string;
  change_action?: string;
  current_summary?: string;
  expected_summary?: string;
  affected_fields?: string[];
  preserved_scope?: string[];
  managed_diff?: string[];
  before_hash?: string | null;
  after_hash?: string | null;
}

export interface MaintenancePreviewData {
  target: MaintenanceTargetSummary;
  platform: MaintenancePlatformData;
  inspection: MaintenanceTargetStateData;
  plan: {
    target_id: string;
    steps: Array<{ step_id: string }>;
    logical_leaves: string[];
    before_fingerprint: string;
    after_fingerprint: string;
    preview_digest: string;
    differences?: MaintenanceDiffData[];
    summary_code?: string;
  };
  /** 生成可应用预览的部署会返回一次性确认材料；只在内存中短暂使用。 */
  change_id?: string;
  confirmation_token?: string;
  expires_at?: string | null;
  expires_in_seconds?: number;
  change?: MaintenanceChangeData;
}

/** 维护事务的公开状态；未知未来值保留为字符串并按安全未知态展示。 */
export type MaintenanceChangeStatus = 'prepared' | 'applying' | 'verifying' | 'succeeded' | 'rolling_back' | 'rolled_back' | 'recovery_required' | string;

/** GET maintenance change 的安全投影，不含备份、路径、命令、正文或环境值。 */
export interface MaintenanceChangeData {
  change_id: string;
  target_id: MaintenanceTargetId;
  action_id?: string;
  risk_level?: string;
  status: MaintenanceChangeStatus;
  base_fingerprint?: string;
  before_fingerprint?: string;
  after_fingerprint?: string;
  step_summary?: string[];
  task_id?: string | null;
  rollback_status?: string;
  restart_required?: boolean;
  created_at?: string;
  expires_at?: string;
  updated_at?: string;
  preview_digest?: string;
  leaf_states?: MaintenanceLeafData[];
}

/** 维护变更关联的任务安全摘要；任务详情仍通过任务中心读取。 */
export interface MaintenanceChangeTask {
  task_id?: string;
  status?: string;
  action_id?: string;
  change_id?: string;
  created_at?: string | null;
}

/** GET /maintenance/changes/{change_id} 的兼容安全包装。 */
export interface MaintenanceChangeEnvelope {
  change: MaintenanceChangeData;
  task?: MaintenanceChangeTask | null;
  blocked?: boolean;
  recovery_required?: boolean;
  warning?: string | null;
}

/** POST maintenance apply 的最小安全响应；不接受或回显配置正文。 */
export interface MaintenanceApplyData {
  change_id: string;
  task_id?: string | null;
  status?: MaintenanceChangeStatus;
  restart_required?: boolean;
  change?: MaintenanceChangeData;
  task?: MaintenanceChangeTask | null;
  blocked?: boolean;
  warning?: string | null;
}
