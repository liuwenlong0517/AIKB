import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DataMaintenancePage } from '../src/pages/DataMaintenancePage';

const mocks = vi.hoisted(() => ({ overview: vi.fn(), preview: vi.fn(), apply: vi.fn() }));
vi.mock('../src/hooks/useApi', () => ({
  useDataMaintenanceOverview: mocks.overview,
  usePreviewDataMaintenance: mocks.preview,
  useApplyDataMaintenance: mocks.apply,
}));

const overview = {
  categories: [
    { id: 'audit', label: '审计数据', retention_days: 90, candidate_count: 2, candidate_bytes: 2048, protected_count: 3 },
    { id: 'archived_work', label: '归档运行任务', retention_days: 180, candidate_count: 1, candidate_bytes: 1024, protected_count: 1 },
    { id: 'web_tasks', label: '终态 Web 任务', retention_days: 30, candidate_count: 1, candidate_bytes: 512, protected_count: 2 },
  ],
  protected: { count: 6, reasons: [{ code: 'within_retention', count: 4 }, { code: 'uncertain_or_active', count: 2 }] },
  defaults: { audit: 90, archived_work: 180, web_tasks: 30 },
  apply_supported: true,
  scan_scope: 'fixed_workspace_categories',
};

const mutation = (mutate = vi.fn()) => ({ mutate, reset: vi.fn(), isPending: false, error: null, data: undefined });

beforeEach(() => {
  mocks.overview.mockReturnValue({ data: { data: overview, meta: {} }, isLoading: false, error: null, refetch: vi.fn() });
  mocks.preview.mockReturnValue(mutation());
  mocks.apply.mockReturnValue(mutation());
});

describe('DataMaintenancePage', () => {
  it('只显示固定类别与保护摘要，不提供路径输入', () => {
    render(<MemoryRouter><DataMaintenancePage /></MemoryRouter>);
    expect(screen.getByText('审计数据')).toBeInTheDocument();
    expect(screen.getByText('归档运行任务')).toBeInTheDocument();
    expect(screen.getByText('终态 Web 任务')).toBeInTheDocument();
    expect(screen.getByText('活动中或状态无法安全确认')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('预览后必须二次确认才执行服务端计划', async () => {
    const preview = {
      plan_id: 'cleanup-one', preview_digest: 'a'.repeat(64), confirmation_token: 'secret-token',
      expires_at: new Date(Date.now() + 60_000).toISOString(), risk_level: 'destructive_local_data',
      categories: overview.categories, candidate_count: 4, candidate_bytes: 3584,
      protected: overview.protected, steps: ['重新扫描固定类别'],
    };
    const previewMutate = vi.fn((_input, options) => options.onSuccess({ data: preview, meta: {} }));
    const applyMutate = vi.fn();
    mocks.preview.mockReturnValue(mutation(previewMutate));
    mocks.apply.mockReturnValue(mutation(applyMutate));
    render(<MemoryRouter><DataMaintenancePage /></MemoryRouter>);
    fireEvent.click(screen.getByRole('button', { name: '生成清理预览' }));
    await waitFor(() => expect(previewMutate).toHaveBeenCalled());
    expect(screen.queryByText('secret-token')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认并执行清理' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: /我已核对类别/ }));
    fireEvent.click(screen.getByRole('button', { name: '确认并执行清理' }));
    expect(applyMutate).toHaveBeenCalledWith({ planId: 'cleanup-one', confirmation_token: 'secret-token' }, expect.anything());
  });
});
