import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AuditPage } from '../src/pages/AuditPage';

const mocks = vi.hoisted(() => ({ summary: vi.fn(), events: vi.fn(), detail: vi.fn() }));
vi.mock('../src/hooks/useApi', () => ({ useAuditSummary: mocks.summary, useAuditEvents: mocks.events, useAuditEvent: mocks.detail }));

const result = <T,>(data: T) => ({ data: { data, meta: { warnings: [] } }, isLoading: false, isError: false, error: null, refetch: vi.fn() });

describe('AuditPage', () => {
  beforeEach(() => {
    mocks.summary.mockReturnValue(result({ count: 0, statuses: {}, agents: {}, sources: {}, operations: {}, fallback_records: 0, damaged_count: 0 }));
    mocks.events.mockReturnValue(result({ items: [], pagination: { page: 1, page_size: 50, total: 0, has_next: false } }));
    mocks.detail.mockReturnValue(result({ invocation_id: 'invoke-1', operation: 'search_knowledge', status: 'incomplete', session_id: null, session_label: null }));
  });

  it('显示无匹配审计调用空态和零摘要', () => {
    render(<MemoryRouter initialEntries={['/audit']}><Routes><Route path="/audit" element={<AuditPage />} /></Routes></MemoryRouter>);
    expect(screen.getByText('调用总数')).toBeInTheDocument();
    expect(screen.getByText('没有匹配的审计调用。')).toBeInTheDocument();
  });

  it('详情将缺少 session 和 incomplete 显示为明确状态', () => {
    render(<MemoryRouter initialEntries={['/audit/invoke-1']}><Routes><Route path="/audit/:invocationId" element={<AuditPage />} /></Routes></MemoryRouter>);
    expect(screen.getByText('未完成')).toBeInTheDocument();
    expect(screen.getAllByText('未提供会话 ID').length).toBeGreaterThan(0);
    expect(screen.getByText('未提供会话标签')).toBeInTheDocument();
  });

  it('summary 与列表共享状态和操作筛选，并合并双方降级元数据', async () => {
    mocks.summary.mockReturnValue({ ...result({ count: 1, statuses: { failed: 1 }, agents: {}, sources: {}, operations: { search_knowledge: 1 } }), data: { data: { count: 1, statuses: { failed: 1 }, agents: {}, sources: {}, operations: { search_knowledge: 1 } }, meta: { degraded: true, warnings: ['audit_partial'] } } });
    mocks.events.mockReturnValue({ ...result({ items: [{ invocation_id: 'invoke-1', operation: 'search_knowledge', status: 'failed', action_text: 'safe action' }], pagination: { page: 1, page_size: 50, total: 1, has_next: false } }), data: { data: { items: [{ invocation_id: 'invoke-1', operation: 'search_knowledge', status: 'failed', action_text: 'safe action' }], pagination: { page: 1, page_size: 50, total: 1, has_next: false } }, meta: { degraded: false, warnings: ['damaged_records'] } } });
    render(<MemoryRouter initialEntries={['/audit']}><Routes><Route path="/audit" element={<AuditPage />} /></Routes></MemoryRouter>);
    fireEvent.change(screen.getByLabelText('按操作筛选审计'), { target: { value: 'search_knowledge' } });
    await waitFor(() => expect(mocks.summary).toHaveBeenLastCalledWith(expect.objectContaining({ operation: 'search_knowledge' })));
    expect(screen.getByText(/审计记录部分不可读/)).toBeInTheDocument();
    expect(screen.getByText(/存在损坏记录/)).toBeInTheDocument();
  });
});
