import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { RulesPage } from '../src/pages/RulesPage';

const mocks = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn(), preview: vi.fn(), apply: vi.fn(), change: vi.fn() }));
vi.mock('../src/hooks/useApi', () => ({ useRules: mocks.list, useRule: mocks.detail, usePreviewRule: mocks.preview, useApplyRule: mocks.apply, useRuleChange: mocks.change }));

const query = <T,>(data: T) => ({ data: { data, meta: {} }, isLoading: false, error: null, refetch: vi.fn() });
const mutation = (data?: unknown) => ({ data: data ? { data, meta: {} } : undefined, isPending: false, error: null, mutate: vi.fn(), reset: vi.fn() });
const user = { rule_id: 'user', title: '个人规则', description: '跨 Agent 的个人偏好', readable: true, writable: true, risk_level: 'source_write', max_chars: 800, content_hash: 'a'.repeat(64), revision: 'b'.repeat(7) };
const agent = { rule_id: 'agent', title: 'AIKB Agent 规则', description: '工作协议', readable: true, writable: false, risk_level: 'read_only', max_chars: 8000, content_hash: 'c'.repeat(64), revision: 'd'.repeat(7) };

function renderPage(path = '/rules/user') {
  return render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/rules" element={<RulesPage />} /><Route path="/rules/:ruleId" element={<RulesPage />} /></Routes></MemoryRouter>);
}

describe('RulesPage', () => {
  it('renders four-rule directory and only user gets edit/preview controls', () => {
    mocks.list.mockReturnValue(query({ items: [agent, { ...user, rule_id: 'entry', title: '入口规则', writable: false }, { ...user, rule_id: 'contributing', title: '贡献规则', writable: false }, user] }));
    mocks.detail.mockReturnValue(query({ ...user, content: '# 个人规则\n' }));
    mocks.preview.mockReturnValue(mutation());
    mocks.apply.mockReturnValue(mutation());
    mocks.change.mockReturnValue(query(undefined));
    renderPage();
    expect(screen.getByRole('navigation', { name: '规则目录' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '编辑正文' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('正式写入必须先完成');
  });

  it('uses a controlled textarea and sends the base hash and candidate for preview', async () => {
    const mutate = vi.fn();
    mocks.list.mockReturnValue(query({ items: [user] }));
    mocks.detail.mockReturnValue(query({ ...user, content: '# 个人规则\n' }));
    mocks.preview.mockReturnValue({ ...mutation(), mutate });
    mocks.apply.mockReturnValue(mutation());
    mocks.change.mockReturnValue(query(undefined));
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: '编辑正文' }));
    const editor = screen.getByRole('textbox', { name: '规则正文编辑器' });
    fireEvent.change(editor, { target: { value: '# 修改后的个人规则\n' } });
    fireEvent.click(screen.getByRole('button', { name: '生成完整预览' }));
    await waitFor(() => expect(mutate).toHaveBeenCalledWith({ ruleId: 'user', base_content_hash: 'a'.repeat(64), candidate_content: '# 修改后的个人规则\n' }, expect.anything()));
  });

  it('readonly detail has no edit or preview button and deep route can render', () => {
    mocks.list.mockReturnValue(query({ items: [agent] }));
    mocks.detail.mockReturnValue(query({ ...agent, content: '# AIKB Agent 规则\n' }));
    mocks.preview.mockReturnValue(mutation());
    mocks.apply.mockReturnValue(mutation());
    mocks.change.mockReturnValue(query(undefined));
    renderPage('/rules/agent');
    expect(screen.getByText('只读规则')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '编辑正文' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '生成完整预览' })).not.toBeInTheDocument();
  });

  it('预览成功且完成高风险确认后只提交变更摘要，并展示任务关联', async () => {
    const previewData = { rule_id: 'user', change_id: 'change-1', before_hash: 'a'.repeat(64), after_hash: 'b'.repeat(64), diff: '--- a/USER_RULES.md\n+++ b/USER_RULES.md\n', validation: { valid: true, errors: [] }, preview_digest: 'c'.repeat(64), confirmation_token: 'memory-token', expires_at: '2999-08-30T01:05:00Z', expires_in_seconds: 300 };
    const previewMutate = vi.fn((_input: unknown, options: { onSuccess: (response: { data: typeof previewData }) => void }) => options.onSuccess({ data: previewData }));
    const applyMutate = vi.fn((_input: unknown, options: { onSuccess: (response: { data: { change_id: string; status: string; task_id: string } }) => void }) => options.onSuccess({ data: { change_id: 'change-1', status: 'submitted', task_id: 'task-1' } }));
    mocks.list.mockReturnValue(query({ items: [user] }));
    const refetchDetail = vi.fn();
    mocks.detail.mockReturnValue({ ...query({ ...user, content: '# 个人规则\n' }), refetch: refetchDetail });
    mocks.preview.mockReturnValue({ ...mutation(), mutate: previewMutate });
    mocks.apply.mockReturnValue({ ...mutation(), mutate: applyMutate });
    mocks.change.mockImplementation((changeId: string | undefined) => changeId ? query({ change: { change_id: changeId, status: 'succeeded', task_id: 'task-1' } }) : query(undefined));
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: '编辑正文' }));
    fireEvent.change(screen.getByRole('textbox', { name: '规则正文编辑器' }), { target: { value: '# 修改后的个人规则\n' } });
    fireEvent.click(screen.getByRole('button', { name: '生成完整预览' }));
    expect(await screen.findByText('高风险确认：即将正式写入 USER_RULES.md')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '确认并提交规则应用' }));
    await waitFor(() => expect(applyMutate).toHaveBeenCalledWith({ ruleId: 'user', change_id: 'change-1', confirmation_token: 'memory-token' }, expect.anything()));
    expect(applyMutate).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(refetchDetail).toHaveBeenCalled());
    expect(screen.getByText('规则已成功应用')).toBeInTheDocument();
    expect(screen.getByText(/确认凭据仅保留在当前页面内存/)).toBeInTheDocument();
  });

  it('应用失败提供重新读取入口，清理旧令牌且不自动重放', async () => {
    const refetchDetail = vi.fn();
    const applyError = new Error('审计写入失败');
    mocks.list.mockReturnValue(query({ items: [user] }));
    mocks.detail.mockReturnValue({ ...query({ ...user, content: '# 个人规则\n' }), refetch: refetchDetail });
    mocks.preview.mockReturnValue({ ...mutation(), mutate: vi.fn((_input: unknown, options: { onSuccess: (response: { data: object }) => void }) => options.onSuccess({ data: { rule_id: 'user', change_id: 'change-2', diff: 'diff', validation: { valid: true, errors: [] }, preview_digest: 'c'.repeat(64), confirmation_token: 'memory-token', expires_at: '2999-08-30T01:05:00Z' } })) });
    mocks.apply.mockReturnValue({ ...mutation(), error: applyError, mutate: vi.fn() });
    mocks.change.mockReturnValue(query(undefined));
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: '编辑正文' }));
    fireEvent.change(screen.getByRole('textbox', { name: '规则正文编辑器' }), { target: { value: '# 修改后的个人规则\n' } });
    fireEvent.click(screen.getByRole('button', { name: '生成完整预览' }));
    expect(await screen.findByText('高风险确认：即将正式写入 USER_RULES.md')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '确认并提交规则应用' }));
    expect(await screen.findByRole('button', { name: '重新读取并生成新预览' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重新读取并生成新预览' }));
    expect(refetchDetail).toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '确认并提交规则应用' })).not.toBeInTheDocument();
  });
});
