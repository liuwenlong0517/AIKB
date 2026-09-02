import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MaintenancePage } from '../src/pages/MaintenancePage';
import { api } from '../src/api/client';

const mocks = vi.hoisted(() => ({ targets: vi.fn(), target: vi.fn(), statuses: vi.fn(), preview: vi.fn(), apply: vi.fn(), change: vi.fn() }));
vi.mock('../src/hooks/useApi', () => ({
  useMaintenanceTargets: mocks.targets,
  useMaintenanceTarget: mocks.target,
  useMaintenanceTargetStatuses: mocks.statuses,
  usePreviewMaintenance: mocks.preview,
  useApplyMaintenance: mocks.apply,
  useMaintenanceChange: mocks.change,
}));

const query = <T,>(data: T) => ({ data: { data, meta: {} }, isLoading: false, error: null, refetch: vi.fn() });
const mutation = (mutate = vi.fn()) => ({ data: undefined, isPending: false, error: null, mutate, reset: vi.fn() });

const targetItems = [
  { target_id: 'environment', title: 'AIKB 用户环境', description: '固定环境', risk_level: 'user_config_write', action_id: 'maintenance.environment.update', status: 'missing', base_fingerprint: 'a'.repeat(64) },
  { target_id: 'agent.codex', title: 'Codex 安装修复', description: '固定 Codex', risk_level: 'user_config_write', action_id: 'maintenance.agent.codex.repair', status: 'drifted', base_fingerprint: 'b'.repeat(64) },
  { target_id: 'agent.claude-code', title: 'Claude Code 安装修复', description: '固定 Claude', risk_level: 'user_config_write', action_id: 'maintenance.agent.claude-code.repair', status: 'unsupported' },
];
const statusQuery = (item: typeof targetItems[number]) => query({
  target: item,
  platform: { platform: 'windows', supported: false, inspection_supported: true, preview_supported: true, apply_supported: false },
  status: { target_id: item.target_id, status: item.status, logical_leaves: [], steps: [], base_fingerprint: item.base_fingerprint },
});

function renderPage() {
  return render(<MemoryRouter initialEntries={['/maintenance']}><Routes><Route path="/maintenance" element={<MaintenancePage />} /></Routes></MemoryRouter>);
}

beforeEach(() => {
  mocks.apply.mockReturnValue(mutation());
  mocks.change.mockReturnValue(query({ change: { change_id: 'maintenance-change-1', target_id: 'environment', status: 'prepared' } }));
});

describe('MaintenancePage', () => {
  it('维护请求只使用固定逻辑目标和基线指纹，并复用安全 POST 头', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { items: [], platform: { platform: 'windows', supported: false } }, meta: {} }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { target: {}, platform: {}, status: {} }, meta: {} }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { target: {}, platform: {}, inspection: {}, plan: {} }, meta: {} }), { status: 200 }));
    await api.maintenance.targets();
    await api.maintenance.target('agent.codex');
    await api.maintenance.preview('agent.codex', { base_fingerprint: 'a'.repeat(64) });
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(['/api/v1/maintenance/targets', '/api/v1/maintenance/targets/agent.codex', '/api/v1/maintenance/targets/agent.codex/preview']);
    expect(fetchMock.mock.calls[2][1]).toEqual(expect.objectContaining({ method: 'POST', headers: expect.objectContaining({ 'Content-Type': 'application/json', 'X-AIKB-Request': '1' }), body: JSON.stringify({ base_fingerprint: 'a'.repeat(64) }) }));
    fetchMock.mockRestore();
  });

  it('显示三个固定目标、状态和只读边界，不显示应用或任意配置输入', () => {
    mocks.targets.mockReturnValue(query({ items: targetItems, platform: { platform: 'windows', supported: false, inspection_supported: true, preview_supported: true, apply_supported: false } }));
    mocks.statuses.mockReturnValue(targetItems.map(statusQuery));
    mocks.target.mockReturnValue(query({ target: targetItems[0], platform: { platform: 'windows', supported: false, inspection_supported: true, preview_supported: true, apply_supported: false }, status: { target_id: 'environment', status: 'missing', logical_leaves: ['user_environment.aikb_home', 'user_environment.aikb_knowledge_home'], steps: [], base_fingerprint: 'a'.repeat(64) }, leaves: [{ leaf_id: 'user_environment.aikb_home', existence: 'missing' }, { leaf_id: 'user_environment.aikb_knowledge_home', existence: 'present' }] }));
    mocks.preview.mockReturnValue(mutation());
    renderPage();
    expect(screen.getByRole('navigation', { name: '维护目标目录' })).toBeInTheDocument();
    expect(screen.getByText('环境')).toBeInTheDocument();
    expect(screen.getByText('Codex')).toBeInTheDocument();
    expect(screen.getByText('Claude Code')).toBeInTheDocument();
    expect(screen.getAllByText('尚未安装').length).toBeGreaterThan(0);
    expect(screen.getByText('检测到漂移')).toBeInTheDocument();
    expect(screen.getByText('当前平台不支持')).toBeInTheDocument();
    expect(screen.getByText('当前仅开放状态查看和结构化预览')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /应用|修复|卸载/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    const operationArea = screen.getByRole('navigation', { name: '维护目标目录' }).closest('.maintenance-layout');
    const notices = screen.getByText('当前仅开放状态查看和结构化预览').closest('.maintenance-post-notices');
    if (!operationArea || !notices) throw new Error('操作区或提示区未渲染');
    expect(operationArea.compareDocumentPosition(notices) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('只用详情基线指纹请求结构化预览，并只渲染受管叶子摘要', async () => {
    const mutate = vi.fn((_input: unknown, options: { onSuccess: (response: { data: object }) => void }) => options.onSuccess({ data: {
    target: targetItems[0], platform: { platform: 'windows', supported: false, inspection_supported: true, preview_supported: true, apply_supported: false }, inspection: { target_id: 'environment', status: 'missing', logical_leaves: ['user_environment.aikb_home', 'user_environment.aikb_knowledge_home'], steps: [], base_fingerprint: 'a'.repeat(64) }, plan: { target_id: 'environment', preview_digest: 'c'.repeat(64), before_fingerprint: 'a'.repeat(64), after_fingerprint: 'b'.repeat(64), steps: [{ step_id: 'preflight' }, { step_id: 'backup' }, { step_id: 'write_environment' }, { step_id: 'verify' }], logical_leaves: ['user_environment.aikb_home', 'user_environment.aikb_knowledge_home'], differences: [{ leaf_id: 'user_environment.aikb_home', difference_code: 'drifted' }] },
    } }));
    mocks.targets.mockReturnValue(query({ items: targetItems, platform: { platform: 'windows', supported: false, inspection_supported: true, preview_supported: true, apply_supported: false } }));
    mocks.statuses.mockReturnValue(targetItems.map(statusQuery));
    mocks.target.mockReturnValue(query({ target: targetItems[0], platform: { platform: 'windows', supported: false, inspection_supported: true, preview_supported: true, apply_supported: false }, status: { target_id: 'environment', status: 'missing', logical_leaves: ['user_environment.aikb_home', 'user_environment.aikb_knowledge_home'], steps: [], base_fingerprint: 'a'.repeat(64) }, leaves: [{ leaf_id: 'user_environment.aikb_home', existence: 'present', progress: 'verified' }, { leaf_id: 'user_environment.aikb_knowledge_home', existence: 'present', progress: 'verified' }] }));
    mocks.preview.mockReturnValue(mutation(mutate));
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: '查看结构化预览' }));
    await waitFor(() => expect(mutate).toHaveBeenCalledWith({ targetId: 'environment', base_fingerprint: 'a'.repeat(64) }, expect.anything()));
    expect(await screen.findByText('结构化预览已生成')).toBeInTheDocument();
    expect(screen.getByText('受管内容将更新')).toBeInTheDocument();
    expect(screen.queryByText(/C:\\|\/Users\\|备份路径|环境变量值/)).not.toBeInTheDocument();
  });

  it('按受管差异语义展示当前问题、动作、影响字段和保留范围', async () => {
    const mutate = vi.fn((_input: unknown, options: { onSuccess: (response: { data: object }) => void }) => options.onSuccess({ data: {
      target: targetItems[0], platform: { platform: 'windows', supported: false, inspection_supported: true, preview_supported: true, apply_supported: false }, inspection: { target_id: 'environment', status: 'missing', logical_leaves: [], steps: [], base_fingerprint: 'a'.repeat(64) }, plan: { target_id: 'environment', preview_digest: 'c'.repeat(64), before_fingerprint: 'a'.repeat(64), after_fingerprint: 'b'.repeat(64), steps: [{ step_id: 'preflight' }], logical_leaves: [], differences: [{ leaf_id: 'user_environment.aikb_home', difference_code: 'drifted', display_name: 'AIKB 控制仓环境设置', current_summary: '受管内容与当前版本不一致', change_action: '更新受管内容', expected_summary: '替换为当前版本的受管内容', affected_fields: ['用户级 AIKB 控制仓设置'], managed_diff: ['控制仓设置的受管值'], preserved_scope: ['其他用户环境变量'], before_hash: 'd'.repeat(64), after_hash: 'e'.repeat(64) }] },
    } }));
    const platform = { platform: 'windows', supported: false, inspection_supported: true, preview_supported: true, apply_supported: false };
    mocks.targets.mockReturnValue(query({ items: targetItems, platform }));
    mocks.statuses.mockReturnValue(targetItems.map(statusQuery));
    mocks.target.mockReturnValue(query({ target: targetItems[0], platform, status: { target_id: 'environment', status: 'missing', logical_leaves: ['user_environment.aikb_home', 'user_environment.aikb_knowledge_home'], steps: [], base_fingerprint: 'a'.repeat(64) }, leaves: [] }));
    mocks.preview.mockReturnValue(mutation(mutate));
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: '查看结构化预览' }));
    expect(await screen.findByText('AIKB 控制仓环境设置')).toBeInTheDocument();
    expect(screen.getByText('当前问题')).toBeInTheDocument();
    expect(screen.getByText('更新受管内容')).toBeInTheDocument();
    expect(screen.getByText('用户级 AIKB 控制仓设置')).toBeInTheDocument();
    expect(screen.getByText('其他用户环境变量')).toBeInTheDocument();
    expect(screen.getByText('显示摘要证据')).toBeInTheDocument();
  });

  it('冲突和损坏目标不提供预览按钮', () => {
    const items = targetItems.map((item) => item.target_id === 'environment' ? { ...item, status: 'conflict' } : item);
    mocks.targets.mockReturnValue(query({ items, platform: { platform: 'windows', supported: false, inspection_supported: true, preview_supported: true, apply_supported: false } }));
    mocks.statuses.mockReturnValue(items.map(statusQuery));
    mocks.target.mockReturnValue(query({ target: items[0], platform: { platform: 'windows', supported: false, inspection_supported: true, preview_supported: true, apply_supported: false }, status: { target_id: 'environment', status: 'conflict', logical_leaves: ['user_environment.aikb_home', 'user_environment.aikb_knowledge_home'], steps: [], base_fingerprint: 'a'.repeat(64) } }));
    mocks.preview.mockReturnValue(mutation());
    renderPage();
    expect(screen.getByRole('button', { name: '查看结构化预览' })).toBeDisabled();
    expect(screen.getByText('当前目标存在冲突，无法安全预览。')).toBeInTheDocument();
  });

  it.each([
    ['ready', '当前状态已就绪，无需预览。'],
    ['invalid', '当前目标配置无效，无法安全预览。'],
    ['unsupported', '当前目标或平台不支持结构化预览。'],
    ['restart_required', '配置已写入，请手动重启对应 Agent，无需再次预览。'],
  ] as const)('%s 状态禁用预览并显示安全原因', (status, reason) => {
    const item = { ...targetItems[0], status };
    const platform = { platform: 'windows', supported: false, inspection_supported: true, preview_supported: true, apply_supported: false };
    mocks.targets.mockReturnValue(query({ items: [item, targetItems[1], targetItems[2]], platform }));
    mocks.statuses.mockReturnValue([statusQuery(item), statusQuery(targetItems[1]), statusQuery(targetItems[2])]);
    mocks.target.mockReturnValue(query({ target: item, platform, status: { target_id: item.target_id, status, logical_leaves: [], steps: [], base_fingerprint: 'a'.repeat(64), restart_required: status === 'restart_required' } }));
    mocks.preview.mockReturnValue(mutation());
    renderPage();
    expect(screen.getByRole('button', { name: '查看结构化预览' })).toBeDisabled();
    expect(screen.getByText(reason)).toBeInTheDocument();
  });

  it('missing 和 drifted 状态允许发起结构化预览', () => {
    const statuses = ['missing', 'drifted'] as const;
    const items = targetItems.map((item, index) => ({ ...item, status: statuses[index] ?? item.status }));
    const platform = { platform: 'windows', supported: false, inspection_supported: true, preview_supported: true, apply_supported: false };
    mocks.targets.mockReturnValue(query({ items, platform }));
    mocks.statuses.mockReturnValue(items.map(statusQuery));
    mocks.target.mockImplementation((targetId: string) => {
      const item = items.find((candidate) => candidate.target_id === targetId) ?? items[0];
      return query({ target: item, platform, status: { target_id: item.target_id, status: item.status, logical_leaves: [], steps: [], base_fingerprint: 'a'.repeat(64) } });
    });
    const mutate = vi.fn();
    mocks.preview.mockReturnValue(mutation(mutate));
    renderPage();
    expect(screen.getByRole('button', { name: '查看结构化预览' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: /Codex/ }));
    expect(screen.getByRole('button', { name: '查看结构化预览' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: '查看结构化预览' }));
    expect(mutate).toHaveBeenCalledWith({ targetId: 'agent.codex', base_fingerprint: 'a'.repeat(64) }, expect.anything());
  });

  it('macOS inspection_supported=false 时明确不支持只读检查', () => {
    mocks.targets.mockReturnValue(query({ items: targetItems, platform: { platform: 'macos', supported: false, inspection_supported: false, preview_supported: false, apply_supported: false } }));
    mocks.statuses.mockReturnValue(targetItems.map((item) => query({ target: item, platform: { platform: 'macos', supported: false, inspection_supported: false, preview_supported: false, apply_supported: false }, status: { target_id: item.target_id, status: 'unsupported', logical_leaves: [], steps: [] } })));
    mocks.target.mockReturnValue(query({ target: targetItems[0], platform: { platform: 'macos', supported: false, inspection_supported: false, preview_supported: false, apply_supported: false }, status: { target_id: 'environment', status: 'unsupported', logical_leaves: [], steps: [] } }));
    mocks.preview.mockReturnValue(mutation());
    renderPage();
    expect(screen.getByText('当前平台不支持安装与修复检查')).toBeInTheDocument();
    expect(screen.getByText('当前平台不支持只读检查。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看结构化预览' })).toBeDisabled();
  });

  it('预览后逐目标二次确认并跳转任务，同时显示重启提示', async () => {
    const previewData = {
      target: targetItems[0],
      platform: { platform: 'windows', supported: true, inspection_supported: true, preview_supported: true, apply_supported: true },
      inspection: { target_id: 'environment', status: 'missing', logical_leaves: [], steps: [], base_fingerprint: 'a'.repeat(64) },
      plan: { target_id: 'environment', preview_digest: 'c'.repeat(64), before_fingerprint: 'a'.repeat(64), after_fingerprint: 'b'.repeat(64), steps: [{ step_id: 'preflight' }], logical_leaves: [], differences: [] },
      change_id: 'maintenance-change-1',
      confirmation_token: 'one-time-secret',
      expires_at: new Date(Date.now() + 60_000).toISOString(),
    };
    const previewMutate = vi.fn((_input: unknown, options: { onSuccess: (response: { data: typeof previewData }) => void }) => options.onSuccess({ data: previewData }));
    const applyMutate = vi.fn((_input: unknown, options: { onSuccess: (response: { data: object }) => void }) => options.onSuccess({ data: { change_id: 'maintenance-change-1', status: 'succeeded', task_id: 'task-1', restart_required: true } }));
    const items = targetItems.map((item) => item.target_id === 'environment' ? { ...item, status: 'missing' } : item);
    const platform = { platform: 'windows', supported: true, inspection_supported: true, preview_supported: true, apply_supported: true };
    mocks.targets.mockReturnValue(query({ items, platform }));
    mocks.statuses.mockReturnValue(items.map(statusQuery));
    mocks.target.mockReturnValue(query({ target: items[0], platform, status: { target_id: 'environment', status: 'missing', logical_leaves: [], steps: [], base_fingerprint: 'a'.repeat(64) }, leaves: [] }));
    mocks.preview.mockReturnValue(mutation(previewMutate));
    mocks.apply.mockReturnValue(mutation(applyMutate));
    mocks.change.mockReturnValue(query({ change: { change_id: 'maintenance-change-1', target_id: 'environment', status: 'succeeded', task_id: 'task-1', restart_required: true } }));
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: '查看结构化预览' }));
    expect(await screen.findByText('高风险确认：即将写入用户配置')).toBeInTheDocument();
    expect(screen.queryByText('one-time-secret')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '确认并应用当前目标' }));
    await waitFor(() => expect(applyMutate).toHaveBeenCalledWith({ changeId: 'maintenance-change-1', confirmation_token: 'one-time-secret' }, expect.anything()));
    expect(screen.getByText('维护已成功应用')).toBeInTheDocument();
    expect(screen.getByText('配置已更新，需要人工重启对应 Agent')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '查看任务中心' })).toHaveAttribute('href', '/tasks/task-1');
  });

  it('维护目标切换后晚到的旧预览响应不会显示或进入确认流', async () => {
    let resolvePreview: ((response: { data: object }) => void) | undefined;
    const previewMutate = vi.fn((_input: unknown, options: { onSuccess: (response: { data: object }) => void }) => { resolvePreview = options.onSuccess; });
    const platform = { platform: 'windows', supported: true, inspection_supported: true, preview_supported: true, apply_supported: true };
    const applyMutate = vi.fn();
    mocks.apply.mockReturnValue(mutation(applyMutate));
    mocks.targets.mockReturnValue(query({ items: targetItems, platform }));
    mocks.statuses.mockReturnValue(targetItems.map((item) => query({ target: item, platform, status: { target_id: item.target_id, status: item.status, logical_leaves: [], steps: [], base_fingerprint: item.base_fingerprint } })));
    mocks.target.mockImplementation((targetId: string) => { const item = targetItems.find((candidate) => candidate.target_id === targetId) ?? targetItems[0]; return query({ target: item, platform, status: { target_id: item.target_id, status: item.status, logical_leaves: [], steps: [], base_fingerprint: item.base_fingerprint }, leaves: [] }); });
    mocks.preview.mockReturnValue(mutation(previewMutate));
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: '查看结构化预览' }));
    fireEvent.click(screen.getByRole('button', { name: /Codex agent\.codex/ }));
    resolvePreview?.({ data: { plan: { target_id: 'environment', preview_digest: 'stale-digest', differences: [] }, target: targetItems[0], platform, inspection: {}, change_id: 'stale-change', confirmation_token: 'stale-token' } });
    await waitFor(() => expect(screen.getByText('Codex')).toBeInTheDocument());
    expect(screen.queryByText('stale-digest')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '确认并应用当前目标' })).not.toBeInTheDocument();
    expect(applyMutate).not.toHaveBeenCalled();
  });
});
