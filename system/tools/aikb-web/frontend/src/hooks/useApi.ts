import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { SearchFilters } from '../types/api';
import type { UseQueryResult } from '@tanstack/react-query';
import type { ApiResponse, AuditEvent, AuditListData, AuditSummaryData, CheckpointDetail, CheckpointListData, RuntimeListData, WorkingStateDetail, ActionsData, TaskData, TaskEvent, TaskSnapshot, TasksData, RuleDetail, RulePreviewData, RulesData, RuleApplyData, RuleChangeEnvelope, MaintenanceTargetsData, MaintenanceTargetDetail, MaintenancePreviewData, MaintenanceApplyData, MaintenanceChangeEnvelope, ManualData } from '../types/api';
import { TaskEventStream } from '../api/taskEvents';
import { useEffect, useRef, useState } from 'react';
import { useQueries } from '@tanstack/react-query';

type PollingQuery = { state: { data?: unknown; error: unknown; fetchFailureCount: number } };
const POLLING_RETRY_LIMIT = 3;

function getHttpStatus(error: unknown): number | undefined {
  if (!error || typeof error !== 'object') return undefined;
  const status = (error as { httpStatus?: unknown }).httpStatus;
  return typeof status === 'number' ? status : undefined;
}

/** 轮询只对短暂故障有限重试；资源不存在或请求不可恢复时立即停止，避免 404 死循环。 */
function pollingInterval(query: PollingQuery, status: string | undefined, terminalStatuses: readonly string[]): number | false {
  if (status && terminalStatuses.includes(status)) return false;
  const error = query.state.error;
  const httpStatus = getHttpStatus(error);
  if (httpStatus !== undefined && httpStatus >= 400 && httpStatus < 500 && ![408, 429].includes(httpStatus)) return false;
  if (error) {
    const failures = query.state.fetchFailureCount;
    if (failures >= POLLING_RETRY_LIMIT) return false;
    return Math.min(15_000, 2_000 * 2 ** Math.max(0, failures - 1));
  }
  return 2_000;
}

export const useOverview = () => useQuery({ queryKey: ['knowledge-overview'], queryFn: api.overview });
/** 深层手册路由刷新后独立恢复正文；未知逻辑 ID 由后端安全拒绝。 */
export const useManual = (manualId: string | undefined): UseQueryResult<ManualData> =>
  useQuery({ queryKey: ['manual', manualId], queryFn: () => api.manual(manualId as string), enabled: Boolean(manualId) });
export const useKnowledgeTree = () => useQuery({ queryKey: ['knowledge-tree'], queryFn: api.tree });
export const useDocument = (id: string | undefined) =>
  useQuery({ queryKey: ['document', id], queryFn: () => api.document(id as string), enabled: Boolean(id) });
export const useSystem = () => useQuery({ queryKey: ['system'], queryFn: api.system });
export const useSearch = (query: string, filters: SearchFilters) =>
    useQuery({
    queryKey: ['search', query, filters],
    queryFn: () => api.search(query, filters),
    enabled: query.trim().length > 0,
  });

/** 读取固定四项规则目录；服务端返回的摘要不含物理路径。 */
export const useRules = (): UseQueryResult<ApiResponse<RulesData>> =>
  useQuery({ queryKey: ['rules'], queryFn: api.rules });

/** 读取选中规则正文；深层路由刷新时由 ruleId 独立恢复详情。 */
export const useRule = (ruleId: string | undefined): UseQueryResult<ApiResponse<RuleDetail>> =>
  useQuery({ queryKey: ['rule', ruleId], queryFn: () => api.rule(ruleId as string), enabled: Boolean(ruleId) });

/** 生成候选正文的服务端校验、完整 diff 和短期预览凭据，不调用应用接口。 */
export const usePreviewRule = () =>
  useMutation({
    mutationFn: (input: { ruleId: string; base_content_hash: string; candidate_content: string }): Promise<ApiResponse<RulePreviewData>> =>
      api.previewRule(input.ruleId, { base_content_hash: input.base_content_hash, candidate_content: input.candidate_content }),
  });

/** 提交已确认的规则事务；仅允许 ruleId（路由）、change_id 和内存令牌进入请求。 */
export const useApplyRule = () =>
  useMutation({
    mutationFn: (input: { ruleId: string; change_id: string; confirmation_token: string }): Promise<ApiResponse<RuleApplyData>> =>
      api.applyRule(input.ruleId, { change_id: input.change_id, confirmation_token: input.confirmation_token }),
  });

/** 查询规则事务安全状态；应用后轮询到固定终态，刷新页面不会凭本地状态重放 apply。 */
export const useRuleChange = (changeId: string | undefined, enabled = true): UseQueryResult<ApiResponse<RuleChangeEnvelope>> =>
  useQuery<ApiResponse<RuleChangeEnvelope>>({
    queryKey: ['rule-change', changeId],
    queryFn: () => api.ruleChange(changeId as string),
    enabled: Boolean(changeId) && enabled,
    refetchInterval: (query) => {
      const status = query.state.data?.data.change.status;
      return pollingInterval(query, status, ['succeeded', 'expired', 'rejected', 'rolled_back', 'recovery_required']);
    },
  });

/** 读取阶段 4B 三个静态维护目标；列表响应不含物理路径或配置正文。 */
export const useMaintenanceTargets = (): UseQueryResult<ApiResponse<MaintenanceTargetsData>> =>
  useQuery({ queryKey: ['maintenance-targets'], queryFn: api.maintenance.targets });

/** 读取单个维护目标的状态和逻辑叶子，页面不会自行推导配置位置。 */
export const useMaintenanceTarget = (targetId: string | undefined): UseQueryResult<ApiResponse<MaintenanceTargetDetail>> =>
  useQuery({ queryKey: ['maintenance-target', targetId], queryFn: () => api.maintenance.target(targetId as string), enabled: Boolean(targetId) });

/** 并行读取三个固定目标的状态，让目录在首屏即可展示环境、Codex、Claude Code 的状态。 */
export const useMaintenanceTargetStatuses = () => useQueries({
  queries: ['environment', 'agent.codex', 'agent.claude-code'].map((targetId) => ({
    queryKey: ['maintenance-target', targetId],
    queryFn: () => api.maintenance.target(targetId),
  })),
});

/** 生成服务端只读结构化差异；请求只携带服务端返回的基线指纹。 */
export const usePreviewMaintenance = () =>
  useMutation({
    mutationFn: (input: { targetId: string; base_fingerprint: string }): Promise<ApiResponse<MaintenancePreviewData>> =>
      api.maintenance.preview(input.targetId, { base_fingerprint: input.base_fingerprint }),
  });

/** 提交逐目标维护确认；请求体只含一次性令牌，变更 ID 来自服务端预览。 */
export const useApplyMaintenance = () =>
  useMutation({
    mutationFn: (input: { changeId: string; confirmation_token: string }): Promise<ApiResponse<MaintenanceApplyData>> =>
      api.maintenance.apply(input.changeId, { confirmation_token: input.confirmation_token }),
  });

/** 轮询维护事务的任务/恢复状态；终态停止轮询，页面不会自动重放 apply。 */
export const useMaintenanceChange = (changeId: string | undefined, enabled = true): UseQueryResult<ApiResponse<MaintenanceChangeEnvelope>> =>
  useQuery<ApiResponse<MaintenanceChangeEnvelope>>({
    queryKey: ['maintenance-change', changeId],
    queryFn: () => api.maintenance.change(changeId as string),
    enabled: Boolean(changeId) && enabled,
    refetchInterval: (query) => {
      const status = query.state.data?.data.change.status;
      return pollingInterval(query, status, ['expired', 'succeeded', 'rolled_back', 'recovery_required']);
    },
  });

/** 活动 Working State 列表查询；筛选参数进入 query key，保证分页和筛选切换不会复用错误页面。 */
export const useRuntimeWorkingStates = (params: { project_id?: string; status?: string; agent?: string; page?: number; page_size?: number } = {}, enabled = true): UseQueryResult<ApiResponse<RuntimeListData>> =>
  useQuery({ queryKey: ['runtime-working-states', params], queryFn: () => api.runtime.list(params), enabled });

/** 历史 Working State 列表只在历史页签启用，避免普通活动页无谓读取归档数据。 */
export const useRuntimeArchivedWorkingStates = (params: { project_id?: string; status?: string; agent?: string; page?: number; page_size?: number } = {}, enabled = true): UseQueryResult<ApiResponse<RuntimeListData>> =>
  useQuery({ queryKey: ['runtime-archived-working-states', params], queryFn: () => api.runtime.archivedList(params), enabled });

/** 单个活动任务详情，禁用无 workId 的空路由请求。 */
export const useRuntimeWorkingState = (workId: string | undefined, enabled = true): UseQueryResult<ApiResponse<WorkingStateDetail>> =>
  useQuery({ queryKey: ['runtime-working-state', workId], queryFn: () => api.runtime.detail(workId as string), enabled: Boolean(workId) && enabled });

/** 历史任务详情独立读取归档接口，不把终态任务重新解释为活动任务。 */
export const useRuntimeArchivedWorkingState = (workId: string | undefined, enabled = true): UseQueryResult<ApiResponse<WorkingStateDetail>> =>
  useQuery({ queryKey: ['runtime-archived-working-state', workId], queryFn: () => api.runtime.archivedDetail(workId as string), enabled: Boolean(workId) && enabled });

/** 任务检查点摘要分页。 */
export const useRuntimeCheckpoints = (workId: string | undefined, params: { page?: number; page_size?: number } = {}, enabled = true): UseQueryResult<ApiResponse<CheckpointListData>> =>
  useQuery({ queryKey: ['runtime-checkpoints', workId, params], queryFn: () => api.runtime.checkpoints(workId as string, params), enabled: Boolean(workId) && enabled });

/** 历史任务检查点分页查询；详情页通过独立 key 保留刷新深链。 */
export const useRuntimeArchivedCheckpoints = (workId: string | undefined, params: { page?: number; page_size?: number } = {}, enabled = true): UseQueryResult<ApiResponse<CheckpointListData>> =>
  useQuery({ queryKey: ['runtime-archived-checkpoints', workId, params], queryFn: () => api.runtime.archivedCheckpoints(workId as string, params), enabled: Boolean(workId) && enabled });

/** 有限检查点详情，不读取原始工作文件。 */
export const useRuntimeCheckpoint = (workId: string | undefined, checkpointId: string | undefined, enabled = true): UseQueryResult<ApiResponse<CheckpointDetail>> =>
  useQuery({ queryKey: ['runtime-checkpoint', workId, checkpointId], queryFn: () => api.runtime.checkpoint(workId as string, checkpointId as string), enabled: Boolean(workId && checkpointId) && enabled });

/** 历史检查点详情独立读取归档接口。 */
export const useRuntimeArchivedCheckpoint = (workId: string | undefined, checkpointId: string | undefined, enabled = true): UseQueryResult<ApiResponse<CheckpointDetail>> =>
  useQuery({ queryKey: ['runtime-archived-checkpoint', workId, checkpointId], queryFn: () => api.runtime.archivedCheckpoint(workId as string, checkpointId as string), enabled: Boolean(workId && checkpointId) && enabled });

/** 审计摘要查询；summary 与列表分别请求，允许列表局部失败而保留计数。 */
export const useAuditSummary = (params: { since?: string; date?: string; agent?: string; source?: string; status?: string; operation?: string } = {}): UseQueryResult<ApiResponse<AuditSummaryData>> =>
  useQuery({ queryKey: ['audit-summary', params], queryFn: () => api.audit.summary(params) });

/** 审计调用分页列表。 */
export const useAuditEvents = (params: { since?: string; date?: string; agent?: string; source?: string; status?: string; operation?: string; page?: number; page_size?: number } = {}): UseQueryResult<ApiResponse<AuditListData>> =>
  useQuery({ queryKey: ['audit-events', params], queryFn: () => api.audit.events(params) });

/** 审计调用有限详情，按逻辑 invocation_id 查询。 */
export const useAuditEvent = (invocationId: string | undefined): UseQueryResult<ApiResponse<AuditEvent>> =>
  useQuery({ queryKey: ['audit-event', invocationId], queryFn: () => api.audit.detail(invocationId as string), enabled: Boolean(invocationId) });

/** 读取静态动作注册表；动作卡片仅可选择 action_id，不开放自定义参数编辑。 */
export const useActions = (): UseQueryResult<ApiResponse<ActionsData>> =>
  useQuery({ queryKey: ['actions'], queryFn: api.actions });

/** 请求服务端规范化预览和一次性令牌；预览本身不触发执行。 */
export const usePreviewAction = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (actionId: string) => api.previewAction(actionId),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['tasks'] }); },
  });
};

/** 创建已确认的受控任务；调用方只能提交预览返回的四个字段。 */
export const useCreateTask = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { action_id: string; parameters: Record<string, unknown>; preview_digest: string; confirmation_token: string }) => api.createTask(body),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['tasks'] }); },
  });
};

/** 读取任务列表，并让执行后列表能够及时反映新建任务。 */
export const useTasks = (): UseQueryResult<ApiResponse<TasksData>> =>
  useQuery({ queryKey: ['tasks'], queryFn: api.tasks, refetchInterval: 10_000 });

/** 读取单个任务的安全快照。 */
export const useTask = (taskId: string | undefined): UseQueryResult<ApiResponse<TaskData>> =>
  useQuery({
    queryKey: ['task', taskId],
    queryFn: () => api.task(taskId as string),
    enabled: Boolean(taskId),
    // SSE 可能因网络、代理或浏览器限制中断；非终态每两秒轮询一次作为最终状态兜底。
    refetchInterval: (query) => {
      const status = query.state.data?.data.task.status;
      return pollingInterval(query, status, ['succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted']);
    },
  });

/** 取消任务是幂等请求；服务端决定终态任务的当前状态，前端不重复推断。 */
export const useCancelTask = (taskId: string | undefined) => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.cancelTask(taskId as string),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['task', taskId] });
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
    },
  });
};

const MAX_PENDING_TASK_EVENTS = 256;
const MAX_TASK_OUTPUT_CHARS = 2 * 1024 * 1024;

/** 在前端维护有界任务投影；事件队列溢出时以本地 replay_reset 检查点替代历史。 */
function foldTaskEvent(current: TaskSnapshot | undefined, event: TaskEvent): TaskSnapshot | undefined {
  const incoming = event.task ?? event.snapshot;
  if (!current && !incoming) return undefined;
  const next: TaskSnapshot = incoming
    ? event.replay_reset ? { ...incoming } : { ...(current ?? incoming), ...incoming }
    : { ...(current as TaskSnapshot) };
  if (event.status) next.status = event.status;
  if (event.progress !== undefined) next.progress = event.progress;
  if (event.type === 'output') {
    if (event.truncated === true) next.output_truncated = true;
    else next.output = `${next.output ?? ''}${String(event.text ?? event.output ?? '')}`.slice(-MAX_TASK_OUTPUT_CHARS);
  }
  if (event.type === 'result' && event.result !== undefined) next.result = event.result;
  next.last_event_id = event.event_id;
  return next;
}

/**
 * 订阅任务 SSE 并自动恢复；事件只上送给页面，具体状态合并由页面控制，便于审查安全字段。
 * enabled 为 false（例如终态任务）时不会创建连接。
 */
export const useTaskEvents = (taskId: string | undefined, enabled: boolean) => {
  const [eventQueue, setEventQueue] = useState<TaskEvent[]>([]);
  const [error, setError] = useState<Error | null>(null);
  const [connected, setConnected] = useState(false);
  const streamGeneration = useRef(0);
  const projection = useRef<TaskSnapshot>();
  useEffect(() => {
    const currentGeneration = ++streamGeneration.current;
    // 路由复用时清掉上一任务的事件和错误，避免新任务继承旧任务的实时状态。
    setEventQueue([]);
    projection.current = undefined;
    setError(null);
    setConnected(false);
    if (!taskId || !enabled) return undefined;
    const stream = new TaskEventStream();
    const subscription = stream.subscribe(taskId, {
      onOpen: () => { if (currentGeneration === streamGeneration.current) { setConnected(true); setError(null); } },
      // 同一批次的 output/status/result 仍按顺序入队；队列超限时保留完整投影检查点，避免无界增长。
      onEvent: (nextEvent) => {
        if (currentGeneration !== streamGeneration.current) return;
        projection.current = foldTaskEvent(projection.current, nextEvent);
        setEventQueue((current) => {
          const next = [...current, nextEvent];
          if (next.length <= MAX_PENDING_TASK_EVENTS || !projection.current) return next;
          return [{ event_id: nextEvent.event_id, type: 'snapshot', replay_reset: true, snapshot: projection.current }];
        });
      },
      onError: (nextError) => { if (currentGeneration === streamGeneration.current) { setConnected(false); setError(nextError); } },
      onTerminal: () => { if (currentGeneration === streamGeneration.current) setConnected(false); },
    });
    return () => { streamGeneration.current += 1; subscription.close(); setConnected(false); };
  }, [taskId, enabled]);
  return { events: eventQueue, event: eventQueue[eventQueue.length - 1], error, connected };
};
