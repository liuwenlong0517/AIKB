/** Web API 的共同响应包络。 */
export interface ApiMeta {
  request_id?: string;
  api_version?: string;
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

export interface KnowledgeRelation { direction: 'incoming' | 'outgoing'; type: string; target: string }

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
}

export interface CapabilityData {
  platform: { platform: string; supported: boolean; reason?: string };
  read_only: boolean;
  capabilities: Array<{ id: string; supported: boolean; reason?: string }>;
}

export interface SystemData { info: SystemInfoData; capabilities: CapabilityData }
export interface SearchFilters { type?: string; tag?: string }
