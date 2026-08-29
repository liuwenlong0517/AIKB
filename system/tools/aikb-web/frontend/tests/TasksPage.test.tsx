import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { TasksPage } from '../src/pages/TasksPage';

const mocks = vi.hoisted(() => ({
  actions: vi.fn(), preview: vi.fn(), create: vi.fn(), tasks: vi.fn(), task: vi.fn(), cancel: vi.fn(), events: vi.fn(),
}));
vi.mock('../src/hooks/useApi', () => ({
  useActions: mocks.actions, usePreviewAction: mocks.preview, useCreateTask: mocks.create,
  useTasks: mocks.tasks, useTask: mocks.task, useCancelTask: mocks.cancel, useTaskEvents: mocks.events,
}));

const queryResult = <T,>(data: T) => ({ data: { data, meta: {} }, isLoading: false, error: null, refetch: vi.fn() });
const mutationResult = <T,>(data?: T) => ({ data: data ? { data } : undefined, isPending: false, error: null, mutate: vi.fn(), reset: vi.fn() });
const action = (actionId: string, title: string) => ({ action_id: actionId, title, description: '安全读取摘要', supported_platforms: ['windows'], risk_level: 'read_only', effects: ['读取安全投影'], timeout_seconds: 15, parameter_schema: { type: 'object', properties: {}, additionalProperties: false }, confirmation_required: false });
function RouteSwitcher() { const navigate = useNavigate(); return <button onClick={() => navigate('/tasks/task-b')}>切换到任务 B</button>; }

describe('TasksPage', () => {
  beforeEach(() => {
    mocks.actions.mockReturnValue(queryResult({ items: [action('validate.structure', '结构校验'), action('repository.status.control', '控制仓状态'), action('repository.status.knowledge', '知识仓状态')] }));
    mocks.tasks.mockReturnValue(queryResult({ items: [], total: 0 }));
    mocks.task.mockReturnValue(queryResult({ task: { task_id: 'task-1', action_id: 'validate.structure', status: 'queued', risk_level: 'read_only', timeout_seconds: 120 } }));
    mocks.events.mockReturnValue({ events: [], event: undefined, connected: false, error: null });
    mocks.preview.mockReturnValue(mutationResult({ preview: { action_id: 'validate.structure', parameters: {}, steps: ['读取双仓安全摘要'], risk_level: 'read_only', effects: ['只读'], timeout_seconds: 120, preview_digest: 'digest' }, confirmation_token: 'secret-token', expires_in_seconds: 300 }));
    mocks.create.mockReturnValue(mutationResult());
    mocks.cancel.mockReturnValue(mutationResult());
  });

  it('列出三项动作并要求先查看只读预览，不显示二次危险确认', () => {
    render(<MemoryRouter initialEntries={['/tasks']}><Routes><Route path="/tasks" element={<TasksPage />} /></Routes></MemoryRouter>);
    expect(screen.getByText('可用动作')).toBeInTheDocument();
    expect(screen.getByText('结构校验')).toBeInTheDocument();
    expect(screen.getByText('控制仓状态')).toBeInTheDocument();
    expect(screen.getByText('知识仓状态')).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole('button', { name: '查看并预览' })[0]);
    expect(screen.getByText('这是只读动作')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '执行预览' })).toBeInTheDocument();
    expect(screen.queryByText('我已阅读上述预览并确认执行')).not.toBeInTheDocument();
  });

  it('语义步骤使用描述表内联文本，避免列表留白造成行内容错位', () => {
    render(<MemoryRouter initialEntries={['/tasks']}><Routes><Route path="/tasks" element={<TasksPage />} /></Routes></MemoryRouter>);
    fireEvent.click(screen.getAllByRole('button', { name: '查看并预览' })[0]);
    const steps = screen.getByText('读取双仓安全摘要');
    expect(steps).toHaveClass('task-preview-steps');
    expect(steps.tagName).toBe('SPAN');
    expect(steps.closest('.ant-list')).toBeNull();
  });

  it('执行只读预览时只提交服务端令牌绑定的四个字段', async () => {
    const createMutation = mutationResult();
    mocks.create.mockReturnValue(createMutation);
    render(<MemoryRouter initialEntries={['/tasks']}><Routes><Route path="/tasks" element={<TasksPage />} /></Routes></MemoryRouter>);
    fireEvent.click(screen.getAllByRole('button', { name: '查看并预览' })[0]);
    fireEvent.click(screen.getByRole('button', { name: '执行预览' }));
    await waitFor(() => expect(createMutation.mutate).toHaveBeenCalledWith({ action_id: 'validate.structure', parameters: {}, preview_digest: 'digest', confirmation_token: 'secret-token' }, expect.anything()));
  });

  it('详情展示终态中文状态并在非终态提供幂等取消入口', () => {
    render(<MemoryRouter initialEntries={['/tasks/task-1']}><Routes><Route path="/tasks/:taskId" element={<TasksPage />} /></Routes></MemoryRouter>);
    expect(screen.getAllByText('排队中').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: '取消任务' })).toBeInTheDocument();
  });

  it('详情按顺序合并同一响应块中的快照、多个输出和结果', async () => {
    mocks.events.mockReturnValue({
      events: [
        { event_id: 1, type: 'snapshot', snapshot: { task_id: 'task-1', status: 'running' } },
        { event_id: 2, type: 'output', text: '第一段' },
        { event_id: 3, type: 'output', text: '第二段' },
        { event_id: 4, type: 'output', truncated: true },
        { event_id: 5, type: 'result', status: 'succeeded', result: { ok: true } },
      ],
      event: undefined,
      connected: false,
      error: null,
    });
    render(<MemoryRouter initialEntries={['/tasks/task-1']}><Routes><Route path="/tasks/:taskId" element={<TasksPage />} /></Routes></MemoryRouter>);
    await waitFor(() => expect(screen.getAllByText('成功').length).toBeGreaterThan(0));
    expect(screen.getByText('第一段第二段')).toBeInTheDocument();
    expect(screen.getByText(/"ok": true/)).toBeInTheDocument();
    expect(screen.getByText('输出已达到安全长度上限，部分内容未展示')).toBeInTheDocument();
  });

  it('从任务 A 导航到任务 B 时不混入旧队列且不会跳过 B 的首个事件', async () => {
    const taskResults = {
      'task-a': queryResult({ task: { task_id: 'task-a', action_id: 'action-task-a', status: 'running', risk_level: 'read_only', timeout_seconds: 120 } }),
      'task-b': queryResult({ task: { task_id: 'task-b', action_id: 'action-task-b', status: 'running', risk_level: 'read_only', timeout_seconds: 120 } }),
    };
    const eventResults = {
      'task-a': { events: [{ event_id: 1, type: 'output', text: '旧任务输出一' }, { event_id: 2, type: 'output', text: '旧任务输出二' }], event: undefined, connected: false, error: null },
      'task-b': { events: [{ event_id: 1, type: 'output', text: '新任务输出' }], event: undefined, connected: false, error: null },
    };
    mocks.task.mockImplementation((taskId: string) => taskResults[taskId as keyof typeof taskResults]);
    mocks.events.mockImplementation((taskId: string) => eventResults[taskId as keyof typeof eventResults]);
    render(<MemoryRouter initialEntries={['/tasks/task-a']}><Routes><Route path="/tasks/:taskId" element={<><TasksPage /><RouteSwitcher /></>} /></Routes></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('旧任务输出一旧任务输出二')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '切换到任务 B' }));
    await waitFor(() => expect(screen.getByText('新任务输出')).toBeInTheDocument());
    expect(screen.queryByText('旧任务输出一旧任务输出二')).not.toBeInTheDocument();
  });

  it('replay_reset 快照按完整事实替换旧任务字段', async () => {
    const taskResult = queryResult({ task: { task_id: 'task-1', action_id: 'old-action', status: 'running', risk_level: 'read_only', timeout_seconds: 120, output: '旧缓存输出' } });
    mocks.task.mockReturnValue(taskResult);
    mocks.events.mockReturnValue({ events: [{ event_id: 10, type: 'snapshot', replay_reset: true, snapshot: { task_id: 'task-1', action_id: 'new-action', status: 'running' } }], event: undefined, connected: false, error: null });
    render(<MemoryRouter initialEntries={['/tasks/task-1']}><Routes><Route path="/tasks/:taskId" element={<TasksPage />} /></Routes></MemoryRouter>);
    await waitFor(() => expect(screen.getAllByText('new-action').length).toBeGreaterThan(0));
    expect(screen.getByText('暂无输出')).toBeInTheDocument();
    expect(screen.queryByText('旧缓存输出')).not.toBeInTheDocument();
  });
});
