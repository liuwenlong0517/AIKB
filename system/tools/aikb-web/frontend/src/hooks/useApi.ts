import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { SearchFilters } from '../types/api';

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
