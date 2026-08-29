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
});
