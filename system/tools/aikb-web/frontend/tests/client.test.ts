import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient, api } from '../src/api/client';

describe('ApiClient', () => {
  afterEach(() => vi.restoreAllMocks());

  it('unwraps the versioned data envelope and encodes query values', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ data: { total: 1 }, meta: { request_id: 'r-1' } }), { status: 200 }));
    const result = await new ApiClient().get<{ total: number }>('/search', { q: '中文 文档' });
    expect(result).toEqual({ total: 1 });
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/search?q=%E4%B8%AD%E6%96%87+%E6%96%87%E6%A1%A3', expect.anything());
  });

  it('exposes structured backend errors', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ error: { code: 'INDEX_DOWN', message: '索引不可用' }, meta: { request_id: 'r-2' } }), { status: 503 }));
    await expect(new ApiClient().get('/system')).rejects.toMatchObject({ message: '索引不可用', code: 'INDEX_DOWN', requestId: 'r-2' });
  });

  it('uses the versioned knowledge routes and unwraps the tree root', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ data: { root: { name: 'content', path: 'content', kind: 'directory', children: [] } }, meta: {} }), { status: 200 }));
    const root = await api.tree();
    expect(root.path).toBe('content');
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/knowledge/tree', expect.anything());
  });

  it('combines system info and capabilities without inventing a dashboard endpoint', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { platform: { name: 'windows', architecture: 'amd64' }, python: { version: '3.11' }, repositories: { control: { available: true }, knowledge: { available: true } }, index: {} }, meta: {} }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { platform: { platform: 'windows', supported: true }, read_only: true, capabilities: [] }, meta: {} }), { status: 200 }));
    const result = await api.system();
    expect(result.capabilities.read_only).toBe(true);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(['/api/v1/system/info', '/api/v1/system/capabilities']);
  });

  it('受控变更请求固定发送 JSON 和 X-AIKB-Request 标记，不扩展自定义参数', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { preview: { action_id: 'validate.structure', parameters: {}, risk_level: 'read_only', effects: [], steps: [], timeout_seconds: 120, preview_digest: 'digest' }, confirmation_token: 'token', expires_in_seconds: 300 }, meta: {} }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { task: { task_id: 'task-1', action_id: 'validate.structure', status: 'queued' } }, meta: {} }), { status: 200 }));
    await api.previewAction('validate.structure');
    await api.createTask({ action_id: 'validate.structure', parameters: {}, preview_digest: 'digest', confirmation_token: 'token' });
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({ method: 'POST', headers: expect.objectContaining({ 'Content-Type': 'application/json', 'X-AIKB-Request': '1' }), body: '{"parameters":{}}' }));
    expect(fetchMock.mock.calls[1][1]).toEqual(expect.objectContaining({ method: 'POST', headers: expect.objectContaining({ 'Content-Type': 'application/json', 'X-AIKB-Request': '1' }), body: '{"action_id":"validate.structure","parameters":{},"preview_digest":"digest","confirmation_token":"token"}' }));
  });

  it('按规则 ID 编码详情路由，并发送候选正文预览契约', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { items: [] }, meta: {} }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { rule_id: 'user', content: '# 用户规则\n', content_hash: 'a'.repeat(64), revision: 'b'.repeat(7), title: '个人规则', description: '', readable: true, writable: true, risk_level: 'source_write', max_chars: 800 }, meta: {} }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { rule_id: 'user', change_id: 'change-1', diff: '--- a/user\n+++ b/user\n', preview_digest: 'c'.repeat(64), confirmation_token: 'secret', expires_at: '2026-08-30T01:05:00Z' }, meta: {} }), { status: 200 }));
    await api.rules();
    await api.rule('user');
    await api.previewRule('user', { base_content_hash: 'a'.repeat(64), candidate_content: '# 用户规则修改\n' });
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(['/api/v1/rules', '/api/v1/rules/user', '/api/v1/rules/user/preview']);
    expect(fetchMock.mock.calls[2][1]).toEqual(expect.objectContaining({ method: 'POST', body: JSON.stringify({ base_content_hash: 'a'.repeat(64), candidate_content: '# 用户规则修改\n' }) }));
  });
});
