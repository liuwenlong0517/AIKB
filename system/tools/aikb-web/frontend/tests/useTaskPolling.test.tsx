import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useTask, useTaskEvents } from '../src/hooks/useApi';

const mocks = vi.hoisted(() => ({
  task: vi.fn(),
  subscriptions: [] as Array<{ handlers: { onError?: (error: Error) => void }; close: ReturnType<typeof vi.fn> }>,
}));
vi.mock('../src/api/client', () => ({ api: { task: mocks.task } }));
vi.mock('../src/api/taskEvents', () => ({
  TaskEventStream: class {
    subscribe(_taskId: string, handlers: typeof mocks.subscriptions[number]['handlers']) {
      const subscription = { handlers, close: vi.fn() };
      mocks.subscriptions.push(subscription);
      queueMicrotask(() => handlers.onError?.(new Error('SSE 连接中断')));
      return subscription;
    }
  },
}));

function Probe() {
  const task = useTask('task-1');
  const events = useTaskEvents('task-1', true);
  return <><output data-testid="status">{task.data?.data.task.status ?? ''}</output><output data-testid="sse-error">{events.error?.message ?? ''}</output></>;
}

describe('useTask polling fallback', () => {
  beforeEach(() => { mocks.task.mockReset(); mocks.subscriptions.length = 0; });

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
});
