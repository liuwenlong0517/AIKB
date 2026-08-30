import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { RulesPage } from '../src/pages/RulesPage';

const mocks = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn(), preview: vi.fn() }));
vi.mock('../src/hooks/useApi', () => ({ useRules: mocks.list, useRule: mocks.detail, usePreviewRule: mocks.preview }));

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
    renderPage();
    expect(screen.getByRole('navigation', { name: '规则目录' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '编辑正文' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('当前批次不提供保存、应用或确认执行按钮');
  });

  it('uses a controlled textarea and sends the base hash and candidate for preview', async () => {
    const mutate = vi.fn();
    mocks.list.mockReturnValue(query({ items: [user] }));
    mocks.detail.mockReturnValue(query({ ...user, content: '# 个人规则\n' }));
    mocks.preview.mockReturnValue({ ...mutation(), mutate });
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
    renderPage('/rules/agent');
    expect(screen.getByText('只读规则')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '编辑正文' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '生成完整预览' })).not.toBeInTheDocument();
  });
});
