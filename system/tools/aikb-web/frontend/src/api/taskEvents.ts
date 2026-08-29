import type { TaskEvent, TaskEventType } from '../types/api';

const EVENT_TYPES = new Set<TaskEventType>(['snapshot', 'status', 'progress', 'output', 'result', 'heartbeat']);

export interface TaskEventStreamOptions {
  fetchImpl?: typeof fetch;
  retryDelayMs?: number;
  sleep?: (milliseconds: number) => Promise<void>;
}

export interface TaskEventSubscription {
  close: () => void;
}

interface TaskEventHandlers {
  onOpen?: () => void;
  onEvent: (event: TaskEvent) => void;
  onError?: (error: Error) => void;
  onTerminal?: () => void;
}

/**
 * 可注入的 SSE 读取器：用 fetch 发送 Last-Event-ID，避免原生 EventSource 无法设置请求头。
 * 连接断开时按最后已接收数字 ID 重连；事件 ID 去重，终态或主动 close 后不再重连。
 */
export class TaskEventStream {
  private readonly fetchImpl: typeof fetch;
  private readonly retryDelayMs: number;
  private readonly sleep: (milliseconds: number) => Promise<void>;

  constructor(options: TaskEventStreamOptions = {}) {
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.retryDelayMs = options.retryDelayMs ?? 1000;
    this.sleep = options.sleep ?? ((milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds)));
  }

  /** 建立任务事件订阅；返回 close 句柄供 React effect 清理。 */
  subscribe(taskId: string, handlers: TaskEventHandlers): TaskEventSubscription {
    const controller = new AbortController();
    let closed = false;
    let lastEventId: number | undefined;
    const seen = new Set<number>();

    const run = async () => {
      while (!closed) {
        try {
          const headers: Record<string, string> = {
            Accept: 'text/event-stream',
            'X-AIKB-Request': '1',
          };
          if (lastEventId !== undefined) headers['Last-Event-ID'] = String(lastEventId);
          const url = `/api/v1/tasks/${encodeURIComponent(taskId)}/events`;
          const response = await this.fetchImpl(url, { headers, signal: controller.signal });
          if (!response.ok) throw new Error(`任务事件暂时不可用（HTTP ${response.status}）`);
          if (!response.body) throw new Error('任务事件流不可用');
          handlers.onOpen?.();
          await this.read(response.body, (event) => {
            // replay_reset 代表服务端给出新的事实游标；旧流的去重历史和过大的游标都必须丢弃。
            if (event.replay_reset) {
              seen.clear();
              lastEventId = event.event_id;
            } else if (seen.has(event.event_id)) return;
            seen.add(event.event_id);
            // reset 后允许小于旧游标的新事实事件，因此不能使用 Math.max。
            lastEventId = event.event_id;
            handlers.onEvent(event);
            if (event.type === 'result' || ['succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted'].includes(String(event.status))) {
              closed = true;
              handlers.onTerminal?.();
              // 终态已经收到后主动中止 reader，避免服务器继续保持一个无意义的长连接。
              controller.abort();
            }
          });
          if (!closed) await this.sleep(this.retryDelayMs);
        } catch (error) {
          if (closed || (error instanceof Error && error.name === 'AbortError')) break;
          handlers.onError?.(error instanceof Error ? error : new Error('任务事件连接中断'));
          await this.sleep(this.retryDelayMs);
        }
      }
    };
    void run();
    return { close: () => { closed = true; controller.abort(); } };
  }

  /** 解析标准 SSE 帧，仅接受冻结契约中的事件类型和 JSON 数据。 */
  private async read(body: ReadableStream<Uint8Array>, onEvent: (event: TaskEvent) => void): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let eventId: number | undefined;
    let eventType = 'message';
    let data: string[] = [];
    const dispatch = () => {
      // 无 data 的帧也要清理临时 id/type；否则下一帧可能继承上一帧的元数据。
      const frameId = eventId;
      const frameType = eventType;
      const frameData = data;
      eventId = undefined;
      eventType = 'message';
      data = [];
      if (!frameData.length) return;
      const parsed = JSON.parse(frameData.join('\n')) as TaskEvent;
      const parsedId = frameId ?? parsed.event_id;
      if (!Number.isInteger(parsedId) || parsedId < 1 || !EVENT_TYPES.has(frameType as TaskEventType)) return;
      onEvent({ ...parsed, event_id: parsedId, type: frameType });
    };
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() ?? '';
      for (const line of lines) {
        if (!line) { dispatch(); continue; }
        if (line.startsWith(':')) continue;
        const separator = line.indexOf(':');
        const field = separator < 0 ? line : line.slice(0, separator);
        const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
        if (field === 'id' && /^\d+$/.test(value)) eventId = Number(value);
        else if (field === 'event') eventType = value;
        else if (field === 'data') data.push(value);
      }
    }
    buffer += decoder.decode();
    if (buffer) data.push(buffer);
    dispatch();
  }
}
