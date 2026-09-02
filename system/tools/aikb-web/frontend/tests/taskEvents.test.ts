import { afterEach, describe, expect, it, vi } from 'vitest';
import { TaskEventStream } from '../src/api/taskEvents';

function responseFor(payload: string): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new TextEncoder().encode(payload)); controller.close(); },
  });
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

function responseForChunks(chunks: string[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const encoder = new TextEncoder();
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

describe('TaskEventStream', () => {
  afterEach(() => vi.restoreAllMocks());

  it('使用 Last-Event-ID 重连并去除重复事件，终态后停止', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseFor('id: 1\nevent: status\ndata: {"event_id":1,"status":"running"}\n\n'))
      .mockResolvedValueOnce(responseFor('id: 1\nevent: status\ndata: {"event_id":1,"status":"running"}\n\nid: 2\nevent: status\ndata: {"event_id":2,"status":"succeeded"}\n\n'));
    const events: number[] = [];
    const stream = new TaskEventStream({ fetchImpl: fetchMock, retryDelayMs: 0, sleep: () => Promise.resolve() });
    stream.subscribe('task-1', { onEvent: (event) => events.push(event.event_id) });
    await vi.waitFor(() => expect(events).toEqual([1, 2]));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][1]).toEqual(expect.objectContaining({ headers: expect.objectContaining({ 'Last-Event-ID': '1', 'X-AIKB-Request': '1' }) }));
  });

  it('同一响应块中的快照、多个输出和结果按顺序全部交付', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(responseFor([
      'id: 1', 'event: snapshot', 'data: {"event_id":1,"type":"snapshot","snapshot":{"task_id":"task-1","status":"queued"}}', '',
      'id: 2', 'event: output', 'data: {"event_id":2,"type":"output","text":"第一段"}', '',
      'id: 3', 'event: output', 'data: {"event_id":3,"type":"output","text":"第二段"}', '',
      'id: 4', 'event: result', 'data: {"event_id":4,"type":"result","status":"succeeded","result":{"ok":true}}', '',
    ].join('\n')));
    const events: Array<{ event_id: number; type: string; text?: string }> = [];
    const stream = new TaskEventStream({ fetchImpl: fetchMock, retryDelayMs: 0, sleep: () => Promise.resolve() });
    stream.subscribe('task-1', { onEvent: (event) => events.push({ event_id: event.event_id, type: event.type, text: event.text ?? undefined }) });
    await vi.waitFor(() => expect(events.map((event) => event.event_id)).toEqual([1, 2, 3, 4]));
    expect(events.map((event) => event.type)).toEqual(['snapshot', 'output', 'output', 'result']);
    expect(events[1].text).toBe('第一段');
    expect(events[2].text).toBe('第二段');
  });

  it('收到 replay_reset 快照时清空旧游标，后续重连使用新的事实游标', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseFor('id: 100\nevent: status\ndata: {"event_id":100,"status":"running"}\n\n'))
      .mockResolvedValueOnce(responseFor([
        'id: 10', 'event: snapshot', 'data: {"event_id":10,"replay_reset":true,"status":"running"}', '',
        'id: 11', 'event: progress', 'data: {"event_id":11,"progress":50}', '',
      ].join('\n')))
      .mockResolvedValueOnce(responseFor('id: 12\nevent: status\ndata: {"event_id":12,"status":"succeeded"}\n\n'));
    const events: number[] = [];
    const stream = new TaskEventStream({ fetchImpl: fetchMock, retryDelayMs: 0, sleep: () => Promise.resolve() });
    stream.subscribe('task-1', { onEvent: (event) => events.push(event.event_id) });
    await vi.waitFor(() => expect(events).toEqual([100, 10, 11, 12]));
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1][1]).toEqual(expect.objectContaining({ headers: expect.objectContaining({ 'Last-Event-ID': '100' }) }));
    expect(fetchMock.mock.calls[2][1]).toEqual(expect.objectContaining({ headers: expect.objectContaining({ 'Last-Event-ID': '11' }) }));
  });

  it('无 data 帧不会把上一帧的 id 和类型污染到下一帧', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(responseFor([
      'id: 99', 'event: result', '',
      'data: {"event_id":1,"status":"running"}', '',
      'id: 1', 'event: status', 'data: {"event_id":1,"status":"running"}', '',
      'id: 2', 'event: status', 'data: {"event_id":2,"status":"succeeded"}', '',
    ].join('\n')));
    const events: number[] = [];
    const stream = new TaskEventStream({ fetchImpl: fetchMock, retryDelayMs: 0, sleep: () => Promise.resolve() });
    stream.subscribe('task-1', { onEvent: (event) => events.push(event.event_id) });
    await vi.waitFor(() => expect(events).toEqual([1, 2]));
  });

  it('terminal status 与 result 分属不同流 chunk 时仍按顺序交付 result', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(responseForChunks([
      'id: 2\nevent: status\ndata: {"event_id":2,"status":"succeeded"}\n\n',
      'id: 3\nevent: result\ndata: {"event_id":3,"status":"succeeded","result":{"ok":true}}\n\n',
    ]));
    const events: string[] = [];
    const stream = new TaskEventStream({ fetchImpl: fetchMock, retryDelayMs: 0, sleep: () => Promise.resolve() });
    stream.subscribe('task-1', { onEvent: (event) => events.push(event.type), onTerminal: () => events.push('terminal') });
    await vi.waitFor(() => expect(events).toEqual(['status', 'result', 'terminal']));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('只有 terminal status 且响应 EOF 时也能结束订阅', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(responseFor('id: 2\nevent: status\ndata: {"event_id":2,"status":"cancelled"}\n\n'));
    const events: string[] = [];
    const stream = new TaskEventStream({ fetchImpl: fetchMock, retryDelayMs: 0, sleep: () => Promise.resolve() });
    stream.subscribe('task-1', { onEvent: (event) => events.push(event.type), onTerminal: () => events.push('terminal') });
    await vi.waitFor(() => expect(events).toEqual(['status', 'terminal']));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
