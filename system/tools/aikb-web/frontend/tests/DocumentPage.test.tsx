import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DocumentPage } from '../src/pages/DocumentPage';
import { useDocument } from '../src/hooks/useApi';

vi.mock('../src/hooks/useApi', () => ({
  useDocument: vi.fn(),
}));

const documentResult = {
  data: {
    id: 'aikb:doc-1',
    title: '部署说明',
    type: 'solution',
    status: 'verified' as const,
    path: 'content/knowledge/deploy.md',
    content: '# 部署说明\n\n正文。',
    tags: ['windows'],
  },
  isLoading: false,
  error: null,
  refetch: vi.fn(),
} as unknown as ReturnType<typeof useDocument>;

describe('DocumentPage', () => {
  beforeEach(() => {
    // Ant Design 的栅格组件依赖浏览器媒体查询；jsdom 没有实现该 API，测试中提供最小兼容桩。
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
    vi.mocked(useDocument).mockReturnValue(documentResult);
  });

  it('renders a button-styled return action and navigates to the knowledge directory', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/knowledge/view?id=aikb%3Adoc-1']}>
        <Routes>
          <Route path="/knowledge/view" element={<DocumentPage />} />
          <Route path="/knowledge" element={<div>知识目录页</div>} />
        </Routes>
      </MemoryRouter>,
    );

    const returnButton = screen.getByRole('button', { name: '← 返回目录' });
    expect(returnButton).toBeInTheDocument();
    await user.click(returnButton);
    expect(await screen.findByText('知识目录页')).toBeInTheDocument();
  });

  it('将关联方向和类型显示为中文，未知枚举使用安全兜底且稳定 ID 仍可点击', () => {
    vi.mocked(useDocument).mockReturnValue({
      ...documentResult,
      data: {
        id: 'aikb:doc-1',
        title: '部署说明',
        type: 'solution',
        status: 'verified' as const,
        path: 'content/knowledge/deploy.md',
        content: '# 部署说明\n\n正文。',
        tags: ['windows'],
        relations: [
          { direction: 'outgoing', type: 'depends_on', target: 'aikb:doc-2' },
          { direction: 'future_internal_direction', type: 'private_relation', target: 'aikb:doc-3' },
        ],
      },
    } as unknown as ReturnType<typeof useDocument>);
    render(
      <MemoryRouter initialEntries={['/knowledge/view?id=aikb%3Adoc-1']}>
        <Routes><Route path="/knowledge/view" element={<DocumentPage />} /></Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole('link', { name: '关联知识（稳定 ID：aikb:doc-2）' })).toBeInTheDocument();
    expect(screen.getByText('关联到（传出） · 依赖')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '关联知识（稳定 ID：aikb:doc-3）' })).toBeInTheDocument();
    expect(screen.getByText('关联方向未说明 · 其他关系')).toBeInTheDocument();
    expect(screen.queryByText('future_internal_direction')).not.toBeInTheDocument();
    expect(screen.queryByText('private_relation')).not.toBeInTheDocument();
  });
});
