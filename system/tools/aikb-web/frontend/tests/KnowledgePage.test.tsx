import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { KnowledgePage } from '../src/pages/KnowledgePage';

const mocks = vi.hoisted(() => ({ tree: vi.fn() }));
vi.mock('../src/hooks/useApi', () => ({ useKnowledgeTree: mocks.tree }));

function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}{useLocation().search}</output>;
}

describe('KnowledgePage', () => {
  beforeEach(() => {
    const children = Array.from({ length: 1_000 }, (_, index) => ({
      id: `aikb:document-${index}`,
      name: `document-${index}.md`,
      title: `知识文档 ${index}`,
      path: `content/knowledge/document-${index}.md`,
      kind: 'document',
    }));
    mocks.tree.mockReturnValue({
      data: { name: 'content', path: 'content', kind: 'directory', children },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
  });

  it('大目录使用固定视口虚拟化，只渲染可见节点', async () => {
    const { container } = render(<MemoryRouter><KnowledgePage /></MemoryRouter>);

    const holder = container.querySelector<HTMLElement>('.ant-tree-list-holder');
    expect(holder).not.toBeNull();
    expect(holder?.style.maxHeight).toBe('480px');
    await waitFor(() => {
      const renderedNodes = container.querySelectorAll('.ant-tree-treenode').length;
      expect(renderedNodes).toBeGreaterThan(0);
      expect(renderedNodes).toBeLessThan(1_001);
    });
  });

  it('虚拟化后仍可选择可见文档并通过逻辑 ID 打开详情', async () => {
    render(
      <MemoryRouter initialEntries={['/knowledge']}>
        <Routes>
          <Route path="*" element={<><KnowledgePage /><LocationProbe /></>} />
        </Routes>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByText('知识文档 0'));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/knowledge/view?id=aikb%3Adocument-0'));
  });
});
