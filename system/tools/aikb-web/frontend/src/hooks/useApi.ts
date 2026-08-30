import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { SearchFilters } from '../types/api';
import type { UseQueryResult } from '@tanstack/react-query';
import type { ApiResponse, AuditEvent, AuditListData, AuditSummaryData, CheckpointDetail, CheckpointListData, RuntimeListData, WorkingStateDetail, ActionsData, TaskData, TaskEvent, TasksData, RuleDetail, RulePreviewData, RulesData } from '../types/api';
import { TaskEventStream } from '../api/taskEvents';
import { useEffect, useRef, useState } from 'react';

export const useOverview = () => useQuery({ queryKey: ['knowledge-overview'], queryFn: api.overview });
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

/** 活动 Working State 列表查询；筛选参数进入 query key，保证分页和筛选切换不会复用错误页面。 */
export const useRuntimeWorkingStates = (params: { project_id?: string; status?: string; agent?: string; page?: number; page_size?: number } = {}): UseQueryResult<ApiResponse<RuntimeListData>> =>
  useQuery({ queryKey: ['runtime-working-states', params], queryFn: () => api.runtime.list(params) });

/** 单个活动任务详情，禁用无 workId 的空路由请求。 */
export const useRuntimeWorkingState = (workId: string | undefined): UseQueryResult<ApiResponse<WorkingStateDetail>> =>
  useQuery({ queryKey: ['runtime-working-state', workId], queryFn: () => api.runtime.detail(workId as string), enabled: Boolean(workId) });

/** 任务检查点摘要分页。 */
export const useRuntimeCheckpoints = (workId: string | undefined, params: { page?: number; page_size?: number } = {}): UseQueryResult<ApiResponse<CheckpointListData>> =>
  useQuery({ queryKey: ['runtime-checkpoints', workId, params], queryFn: () => api.runtime.checkpoints(workId as string, params), enabled: Boolean(workId) });

/** 有限检查点详情，不读取原始工作文件。 */
export const useRuntimeCheckpoint = (workId: string | undefined, checkpointId: string | undefined): UseQueryResult<ApiResponse<CheckpointDetail>> =>
  useQuery({ queryKey: ['runtime-checkpoint', workId, checkpointId], queryFn: () => api.runtime.checkpoint(workId as string, checkpointId as string), enabled: Boolean(workId && checkpointId) });

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
      return status && ['succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted'].includes(status) ? false : 2_000;
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

/**
 * 订阅任务 SSE 并自动恢复；事件只上送给页面，具体状态合并由页面控制，便于审查安全字段。
 * enabled 为 false（例如终态任务）时不会创建连接。
 */
export const useTaskEvents = (taskId: string | undefined, enabled: boolean) => {
  const [eventQueue, setEventQueue] = useState<TaskEvent[]>([]);
  const [error, setError] = useState<Error | null>(null);
  const [connected, setConnected] = useState(false);
  const streamGeneration = useRef(0);
  useEffect(() => {
    const currentGeneration = ++streamGeneration.current;
    // 路由复用时清掉上一任务的事件和错误，避免新任务继承旧任务的实时状态。
    setEventQueue([]);
    setError(null);
    setConnected(false);
    if (!taskId || !enabled) return undefined;
    const stream = new TaskEventStream();
    const subscription = stream.subscribe(taskId, {
      onOpen: () => { if (currentGeneration === streamGeneration.current) { setConnected(true); setError(null); } },
      // 使用函数式更新，React 在同一批次收到多个 SSE 帧时仍会逐个追加，不丢 output/status。
      onEvent: (nextEvent) => { if (currentGeneration === streamGeneration.current) setEventQueue((current) => [...current, nextEvent]); },
      onError: (nextError) => { if (currentGeneration === streamGeneration.current) { setConnected(false); setError(nextError); } },
      onTerminal: () => { if (currentGeneration === streamGeneration.current) setConnected(false); },
    });
    return () => { streamGeneration.current += 1; subscription.close(); setConnected(false); };
  }, [taskId, enabled]);
  return { events: eventQueue, event: eventQueue[eventQueue.length - 1], error, connected };
};
