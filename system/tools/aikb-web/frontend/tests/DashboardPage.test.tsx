import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { DashboardPage } from '../src/pages/DashboardPage';
import { useOverview } from '../src/hooks/useApi';

vi.mock('../src/hooks/useApi', () => ({ useOverview: vi.fn() }));

describe('DashboardPage', () => {
  it('知识总览不可用时仍保留两份使用指南入口', () => {
    vi.mocked(useOverview).mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('index unavailable'),
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useOverview>);

    render(<MemoryRouter><DashboardPage /></MemoryRouter>);

    expect(screen.getByRole('link', { name: '阅读项目手册' })).toHaveAttribute('href', '/manuals/project');
    expect(screen.getByRole('link', { name: '阅读命令手册' })).toHaveAttribute('href', '/manuals/commands');
    expect(screen.queryByText(/README\.md|system\/COMMANDS\.md/)).not.toBeInTheDocument();
    expect(screen.getByText('暂时无法读取数据')).toBeInTheDocument();
  });
});
