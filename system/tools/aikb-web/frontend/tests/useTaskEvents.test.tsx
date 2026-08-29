import { act, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useTaskEvents } from '../src/hooks/useApi';

const streamState = vi.hoisted(() => ({ subscriptions: [] as Array<{ handlers: { onEvent: (event: { event_id: number }) => void; onError?: (error: Error) => void }; close: ReturnType<typeof vi.fn> }> }));
vi.mock('../src/api/taskEvents', () => ({
  TaskEventStream: class {
    subscribe(_taskId: string, handlers: typeof streamState.subscriptions[number]['handlers']) {
      const subscription = { handlers, close: vi.fn() };
      streamState.subscriptions.push(subscription);
      return subscription;
    }
  },
}));

function Probe({ taskId }: { taskId: string }) {
  const state = useTaskEvents(taskId, true);
  return <><output data-testid="events">{state.events.map((event) => event.event_id).join(',')}</output><output data-testid="error">{state.error?.message ?? ''}</output></>;
}

describe('useTaskEvents', () => {
  beforeEach(() => { streamState.subscriptions.length = 0; });

  it('任务 ID 变化时清理事件和错误，并忽略旧流迟到事件', async () => {
    const view = render(<Probe taskId="task-a" />);
    await waitFor(() => expect(streamState.subscriptions).toHaveLength(1));
    act(() => {
      streamState.subscriptions[0].handlers.onEvent({ event_id: 1 });
      streamState.subscriptions[0].handlers.onError?.(new Error('旧流错误'));
    });
    await waitFor(() => { expect(screen.getByTestId('events')).toHaveTextContent('1'); expect(screen.getByTestId('error')).toHaveTextContent('旧流错误'); });

    view.rerender(<Probe taskId="task-b" />);
    await waitFor(() => { expect(streamState.subscriptions).toHaveLength(2); expect(screen.getByTestId('events')).toHaveTextContent(''); expect(screen.getByTestId('error')).toHaveTextContent(''); });
    act(() => {
      streamState.subscriptions[0].handlers.onEvent({ event_id: 99 });
      streamState.subscriptions[1].handlers.onEvent({ event_id: 2 });
    });
    await waitFor(() => expect(screen.getByTestId('events')).toHaveTextContent('2'));
    expect(screen.getByTestId('events')).not.toHaveTextContent('99');
  });
});
