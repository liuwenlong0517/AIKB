import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RuntimePage } from '../src/pages/RuntimePage';

const mocks = vi.hoisted(() => ({
  list: vi.fn(), archivedList: vi.fn(), detail: vi.fn(), archivedDetail: vi.fn(), checkpoints: vi.fn(), archivedCheckpoints: vi.fn(), checkpoint: vi.fn(), archivedCheckpoint: vi.fn(),
}));
vi.mock('../src/hooks/useApi', () => ({
  useRuntimeWorkingStates: mocks.list,
  useRuntimeArchivedWorkingStates: mocks.archivedList,
  useRuntimeWorkingState: mocks.detail,
  useRuntimeArchivedWorkingState: mocks.archivedDetail,
  useRuntimeCheckpoints: mocks.checkpoints,
  useRuntimeArchivedCheckpoints: mocks.archivedCheckpoints,
  useRuntimeCheckpoint: mocks.checkpoint,
  useRuntimeArchivedCheckpoint: mocks.archivedCheckpoint,
}));

const result = <T,>(data: T) => ({ data: { data, meta: { warnings: [] } }, isLoading: false, isError: false, error: null, refetch: vi.fn() });
const emptyList = { items: [], pagination: { page: 1, page_size: 20, total: 0, has_next: false } };

describe('RuntimePage', () => {
  beforeEach(() => {
    mocks.list.mockReturnValue(result(emptyList));
    mocks.archivedList.mockReturnValue(result(emptyList));
    mocks.detail.mockReturnValue(result({ work_id: 'demo-task', status: 'active', goal: '只读观察', session_id: null, repositories: [] }));
    mocks.archivedDetail.mockReturnValue(result({ work_id: 'demo-task', status: 'completed', goal: '只读观察', session_id: null, repositories: [] }));
    mocks.checkpoints.mockReturnValue(result({ items: [], pagination: { page: 1, page_size: 20, total: 0, has_next: false } }));
    mocks.archivedCheckpoints.mockReturnValue(result({ items: [], pagination: { page: 1, page_size: 20, total: 0, has_next: false } }));
    mocks.checkpoint.mockReturnValue(result({ checkpoint_id: 'cp-1', status: 'active', session_id: null, goal: '只读观察', sections: {} }));
    mocks.archivedCheckpoint.mockReturnValue(result({ checkpoint_id: 'cp-1', status: 'active', session_id: null, goal: '只读观察', sections: {} }));
  });

  it('显示活动任务合法空集，不出现写入动作', () => {
    render(<MemoryRouter initialEntries={['/runtime']}><Routes><Route path="/runtime" element={<RuntimePage />} /></Routes></MemoryRouter>);
    expect(screen.getByText('当前没有活动任务。')).toBeInTheDocument();
    expect(screen.queryByText(/创建|执行|修复|下载/)).not.toBeInTheDocument();
  });

  it('支持任务详情和检查点深链，并显示缺失会话提示', () => {
    render(<MemoryRouter initialEntries={['/runtime/demo-task/checkpoints/cp-1']}><Routes><Route path="/runtime/:workId/checkpoints/:checkpointId" element={<RuntimePage />} /></Routes></MemoryRouter>);
    expect(screen.getByText('任务概览')).toBeInTheDocument();
    expect(screen.getAllByText('未提供会话 ID').length).toBeGreaterThan(0);
    expect(screen.getByText('检查点详情')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /返回活动任务/ })).toBeInTheDocument();
  });

  it('检查点列表可翻页并请求后续页，而不是始终读取第一页', async () => {
    mocks.checkpoints.mockImplementation((_, params: { page: number }) => result({
      items: [{ checkpoint_id: params.page === 1 ? 'cp-1' : 'cp-21', status: 'active', session_id: null }],
      pagination: { page: params.page, page_size: 20, total: 21, has_next: params.page === 1 },
    }));
    render(<MemoryRouter initialEntries={['/runtime/demo-task']}><Routes><Route path="/runtime/:workId" element={<RuntimePage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText('cp-1')).toBeInTheDocument();
    fireEvent.click(screen.getByTitle('2'));
    await waitFor(() => expect(mocks.checkpoints).toHaveBeenLastCalledWith('demo-task', { page: 2, page_size: 20 }, true));
    expect(await screen.findByText('cp-21')).toBeInTheDocument();
  });

  it('分开展示 owner、最新作者并标记旧数据归属状态', () => {
    mocks.list.mockReturnValue(result({ items: [{ work_id: 'legacy-task', status: 'active', ownership_mode: 'legacy-unbound', owner_agent: null, author_agent: 'claude-code', agent: 'claude-code', participant_count: 0 }], pagination: { page: 1, page_size: 20, total: 1, has_next: false } }));
    render(<MemoryRouter initialEntries={['/runtime']}><Routes><Route path="/runtime" element={<RuntimePage />} /></Routes></MemoryRouter>);
    expect(screen.getByText('旧数据·未认领')).toBeInTheDocument();
    expect(screen.getByText('Owner：—')).toBeInTheDocument();
    expect(screen.getByText('最新作者：claude-code')).toBeInTheDocument();
  });

  it('历史页签读取终态任务并使用历史深链', async () => {
    mocks.archivedList.mockReturnValue(result({ items: [{ work_id: 'history-task', status: 'completed', lifecycle: 'archived', goal: '已完成任务' }], pagination: { page: 1, page_size: 20, total: 1, has_next: false } }));
    render(<MemoryRouter initialEntries={['/runtime']}><Routes><Route path="/runtime" element={<RuntimePage />} /><Route path="/runtime/history" element={<RuntimePage />} /></Routes></MemoryRouter>);
    fireEvent.click(screen.getByText('历史任务'));
    expect(await screen.findByText('history-task')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '查看详情' })).toHaveAttribute('href', '/runtime/history/history-task');
    expect(mocks.list).toHaveBeenLastCalledWith(expect.any(Object), false);
    expect(mocks.archivedList).toHaveBeenLastCalledWith(expect.any(Object), true);
  });

  it('total 不可用时仍依据 has_next 显示下一页并把页码写入 URL', async () => {
    mocks.list.mockImplementation((params: { page: number }) => result({
      items: [{ work_id: `active-${params.page}`, status: 'active', goal: '只读观察' }],
      pagination: { page: params.page, page_size: 20, total: null, has_next: params.page === 1 },
    }));
    render(<MemoryRouter initialEntries={['/runtime?project=demo&page=1']}><Routes><Route path="/runtime" element={<RuntimePage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText('active-1')).toBeInTheDocument();
    expect(screen.getByText('本页 1 项')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '下一页' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await waitFor(() => expect(mocks.list).toHaveBeenLastCalledWith({ project_id: 'demo', agent: undefined, status: undefined, page: 2, page_size: 20 }, true));
    expect(await screen.findByText('active-2')).toBeInTheDocument();
  });
});
