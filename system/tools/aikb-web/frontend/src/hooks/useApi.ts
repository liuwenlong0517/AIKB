import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { SearchFilters } from '../types/api';
import type { UseQueryResult } from '@tanstack/react-query';
import type { ApiResponse, AuditEvent, AuditListData, AuditSummaryData, CheckpointDetail, CheckpointListData, RuntimeListData, WorkingStateDetail } from '../types/api';

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
