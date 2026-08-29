import type {
  ApiErrorBody,
  ApiResponse,
  CapabilityData,
  DocumentData,
  OverviewData,
  SearchData,
  SearchFilters,
  SystemData,
  SystemInfoData,
  TreeNode,
  AuditEvent,
  AuditListData,
  AuditSummaryData,
  CheckpointDetail,
  CheckpointListData,
  RuntimeListData,
  WorkingStateDetail,
} from '../types/api';

/** 将后端错误统一转换为页面可展示的错误，并保留 request id 供问题定位。 */
export class ApiClientError extends Error {
  readonly code?: string;
  readonly requestId?: string;

  constructor(message: string, code?: string, requestId?: string) {
    super(message);
    this.name = 'ApiClientError';
    this.code = code;
    this.requestId = requestId;
  }
}

/** 封装只读 HTTP 协议，页面不得自行拼接 API 或接触本地事实源。 */
export class ApiClient {
  private readonly baseUrl: string;

  constructor(baseUrl = '/api/v1') {
    this.baseUrl = baseUrl.replace(/\/$/, '');
  }

  /** 发起只读 GET 请求并规范查询参数。 */
  async get<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
    return (await this.getEnvelope<T>(path, params)).data;
  }

  /**
   * 读取完整只读包络，页面据此显示局部降级警告和 request id。
   * 与 get 分开是为了保持阶段 1 API 的返回类型兼容，同时不丢失阶段 2 的可信度元数据。
   */
  async getEnvelope<T>(path: string, params?: Record<string, string | number | undefined>): Promise<ApiResponse<T>> {
    const url = new URL(`${this.baseUrl}${path}`, window.location.origin);
    Object.entries(params ?? {}).forEach(([key, value]) => {
      if (value !== undefined && value !== '') url.searchParams.set(key, String(value));
    });
    try {
      const response = await fetch(url.pathname + url.search, { headers: { Accept: 'application/json' } });
      return this.parseEnvelope<T>(response);
    } catch (error) {
      if (error instanceof ApiClientError) throw error;
      throw new ApiClientError('无法连接 AIKB Web 后端，请确认服务已启动。');
    }
  }

  /** 解析统一包络，拒绝非 JSON 和缺少 data/error 的响应。 */
  private async parseEnvelope<T>(response: Response): Promise<ApiResponse<T>> {
    let body: ApiResponse<T> | ApiErrorBody;
    try {
      body = (await response.json()) as ApiResponse<T> | ApiErrorBody;
    } catch {
      throw new ApiClientError(`服务返回了无法解析的响应（HTTP ${response.status}）`);
    }
    if (!body || typeof body !== 'object') throw new ApiClientError(`服务返回了无效的响应结构（HTTP ${response.status}）`);
    if (!response.ok || 'error' in body) {
      const error = 'error' in body ? body.error : undefined;
      throw new ApiClientError(error?.message ?? `请求失败（HTTP ${response.status}）`, error?.code, body.meta?.request_id);
    }
    if (!('data' in body)) throw new ApiClientError(`服务返回了无效的数据包络（HTTP ${response.status}）`);
    return body as ApiResponse<T>;
  }
}

export const apiClient = new ApiClient();

export const api = {
  overview: () => apiClient.get<OverviewData>('/knowledge/overview'),
  tree: async () => (await apiClient.get<{ root: TreeNode }>('/knowledge/tree')).root,
  search: (query: string, filters: SearchFilters) =>
    apiClient.get<SearchData>('/knowledge/search', { q: query, type: filters.type, tags: filters.tag }),
  document: (idOrPath: string) => apiClient.get<DocumentData>('/knowledge/document', { id_or_path: idOrPath }),
  system: async (): Promise<SystemData> => {
    const [info, capabilities] = await Promise.all([
      apiClient.get<SystemInfoData>('/system/info'),
      apiClient.get<CapabilityData>('/system/capabilities'),
    ]);
    return { info, capabilities };
  },
  /** 阶段 2 Working State 只读资源。ID 使用路径编码，浏览器永不接触物理路径。 */
  runtime: {
    list: (params: { project_id?: string; status?: string; agent?: string; page?: number; page_size?: number } = {}) =>
      apiClient.getEnvelope<RuntimeListData>('/runtime/working-states', params),
    detail: async (workId: string) => {
      const response = await apiClient.getEnvelope<WorkingStateDetail | { item: WorkingStateDetail }>(`/runtime/working-states/${encodeURIComponent(workId)}`);
      return { ...response, data: unwrapItem(response.data) };
    },
    checkpoints: (workId: string, params: { page?: number; page_size?: number } = {}) =>
      apiClient.getEnvelope<CheckpointListData>(`/runtime/working-states/${encodeURIComponent(workId)}/checkpoints`, params),
    checkpoint: async (workId: string, checkpointId: string) => {
      const response = await apiClient.getEnvelope<CheckpointDetail | { item: CheckpointDetail }>(`/runtime/working-states/${encodeURIComponent(workId)}/checkpoints/${encodeURIComponent(checkpointId)}`);
      return { ...response, data: unwrapItem(response.data) };
    },
  },
  /** 阶段 2 审计只读资源；仅传递契约允许的筛选字段。 */
  audit: {
    summary: (params: { since?: string; date?: string; agent?: string; source?: string; status?: string; operation?: string } = {}) =>
      apiClient.getEnvelope<AuditSummaryData>('/audit/summary', params),
    events: (params: { since?: string; date?: string; agent?: string; source?: string; status?: string; operation?: string; page?: number; page_size?: number } = {}) =>
      apiClient.getEnvelope<AuditListData>('/audit/events', params),
    detail: (invocationId: string) =>
      apiClient.getEnvelope<AuditEvent>(`/audit/events/${encodeURIComponent(invocationId)}`),
  },
};

/** 共享核心的详情读模型包含 item 包装；在 API 客户端边界统一摊平，页面不感知后端适配层形状。 */
function unwrapItem<T>(value: T | { item: T }): T {
  if (value && typeof value === 'object' && 'item' in value && value.item) return value.item;
  return value as T;
}
