import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SearchPage } from '../src/pages/SearchPage';
import { useOverview, useSearch } from '../src/hooks/useApi';

vi.mock('../src/hooks/useApi', () => ({
  useOverview: vi.fn(),
  useSearch: vi.fn(),
}));

const overviewResult = {
  data: {
    document_count: 3,
    by_type: { decision: 2, solution: 1 },
    by_tag: [{ tag: 'windows', count: 2 }, { tag: '发布', count: 1 }],
    recent_documents: [],
  },
  isLoading: false,
  isError: false,
} as unknown as ReturnType<typeof useOverview>;

const searchResult = {
  data: { query: '部署', count: 0, results: [] },
  isLoading: false,
  error: null,
  refetch: vi.fn(),
} as unknown as ReturnType<typeof useSearch>;

describe('SearchPage', () => {
  beforeEach(() => {
    vi.mocked(useOverview).mockReturnValue(overviewResult);
    vi.mocked(useSearch).mockReturnValue(searchResult);
  });

  it('explains filter semantics and builds options from overview metadata', async () => {
    const user = userEvent.setup();
    render(<SearchPage />, { wrapper: ({ children }) => <MemoryRouter initialEntries={['/search?q=部署']}>{children}</MemoryRouter> });

    expect(screen.getByText(/两个条件均来自知识库元数据，同时选择时取交集/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '清除筛选' })).toBeDisabled();

    await user.click(screen.getByRole('combobox', { name: '按知识类型筛选' }));
    expect(await screen.findByText('工程决策（2篇）')).toBeInTheDocument();
    expect(screen.getByText('解决方案（1篇）')).toBeInTheDocument();

    await user.click(screen.getByRole('combobox', { name: '按知识标签筛选' }));
    expect(await screen.findByText('windows（2篇）')).toBeInTheDocument();
    expect(screen.getByText('发布（1篇）')).toBeInTheDocument();
  });

  it('passes selected filters to the search hook and can clear them', async () => {
    const user = userEvent.setup();
    render(<SearchPage />, { wrapper: ({ children }) => <MemoryRouter initialEntries={['/search?q=部署']}>{children}</MemoryRouter> });

    await user.click(screen.getByRole('combobox', { name: '按知识类型筛选' }));
    await user.click(await screen.findByText('工程决策（2篇）'));
    await waitFor(() => expect(vi.mocked(useSearch)).toHaveBeenLastCalledWith('部署', { type: 'decision' }));

    await user.click(screen.getByRole('combobox', { name: '按知识标签筛选' }));
    await user.click(await screen.findByText('windows（2篇）'));
    await waitFor(() => expect(vi.mocked(useSearch)).toHaveBeenLastCalledWith('部署', { type: 'decision', tag: 'windows' }));

    await user.click(screen.getByRole('button', { name: '清除筛选' }));
    await waitFor(() => expect(vi.mocked(useSearch)).toHaveBeenLastCalledWith('部署', {}));
    expect(screen.getByRole('button', { name: '清除筛选' })).toBeDisabled();
  });
});
