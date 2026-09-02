import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useEffect, useState } from 'react';
import { useTask, useTaskEvents, useTasks } from '../src/hooks/useApi';

const mocks = vi.hoisted(() => ({
  task: vi.fn(),
  tasks: vi.fn(),
  streamMode: 'error' as 'error' | 'open',
  subscriptions: [] as Array<{ handlers: { onOpen?: () => void; onError?: (error: Error) => void }; close: ReturnType<typeof vi.fn> }>,
}));
vi.mock('../src/api/client', () => ({ api: { task: mocks.task, tasks: mocks.tasks } }));
vi.mock('../src/api/taskEvents', () => ({
  TaskEventStream: class {
    subscribe(_taskId: string, handlers: typeof mocks.subscriptions[number]['handlers']) {
      const subscription = { handlers, close: vi.fn() };
      mocks.subscriptions.push(subscription);
      queueMicrotask(() => mocks.streamMode === 'open' ? handlers.onOpen?.() : handlers.onError?.(new Error('SSE 连接中断')));
      return subscription;
    }
  },
}));

function Probe() {
  const task = useTask('task-1');
  const events = useTaskEvents('task-1', true);
  return <><output data-testid="status">{task.data?.data.task.status ?? ''}</output><output data-testid="sse-error">{events.error?.message ?? ''}</output></>;
}

function ConnectedProbe() {
  const [connected, setConnected] = useState(false);
  const events = useTaskEvents('task-1', true);
  const task = useTask('task-1', connected);
  useEffect(() => setConnected(events.connected), [events.connected]);
  return <output data-testid="connected-status">{task.data?.data.task.status ?? ''}</output>;
}

function TaskListProbe() {
  const result = useTasks();
  return <output data-testid="task-list-status">{result.data?.data.items.length ?? ''}</output>;
}

describe('useTask polling fallback', () => {
  beforeEach(() => { mocks.task.mockReset(); mocks.tasks.mockReset(); mocks.streamMode = 'error'; mocks.subscriptions.length = 0; });

  it('SSE 出错后仍轮询 running 到 succeeded，并在终态停止', async () => {
    mocks.task
      .mockResolvedValueOnce({ data: { task: { task_id: 'task-1', action_id: 'validate.structure', status: 'running' } }, meta: {} })
      .mockResolvedValueOnce({ data: { task: { task_id: 'task-1', action_id: 'validate.structure', status: 'succeeded' } }, meta: {} });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><Probe /></QueryClientProvider>);
    await waitFor(() => expect(screen.getByTestId('sse-error')).toHaveTextContent('SSE 连接中断'));
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('succeeded'), { timeout: 3_500, interval: 50 });
    expect(mocks.task).toHaveBeenCalledTimes(2);
  });

  it('404 资源不会继续启动轮询', async () => {
    mocks.task.mockRejectedValue(Object.assign(new Error('任务不存在'), { httpStatus: 404 }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><Probe /></QueryClientProvider>);
    await waitFor(() => expect(mocks.task).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 2_200));
    expect(mocks.task).toHaveBeenCalledTimes(1);
  });

  it('SSE 已连接时详情不重复轮询', async () => {
    mocks.streamMode = 'open';
    mocks.task.mockResolvedValue({ data: { task: { task_id: 'task-1', action_id: 'validate.structure', status: 'running' } }, meta: {} });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><ConnectedProbe /></QueryClientProvider>);
    await waitFor(() => expect(mocks.subscriptions).toHaveLength(1));
    await new Promise((resolve) => setTimeout(resolve, 2_200));
    expect(mocks.task).toHaveBeenCalledTimes(1);
  });

  it('任务列表为空或全为终态时停止固定 10 秒轮询', async () => {
    vi.useFakeTimers();
    mocks.tasks.mockResolvedValue({ data: { items: [], total: 0 }, meta: {} });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><TaskListProbe /></QueryClientProvider>);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const callsAfterLoad = mocks.tasks.mock.calls.length;
    await act(async () => { vi.advanceTimersByTime(10_001); await Promise.resolve(); });
    expect(callsAfterLoad).toBe(1);
    expect(mocks.tasks).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
