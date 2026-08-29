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
});
