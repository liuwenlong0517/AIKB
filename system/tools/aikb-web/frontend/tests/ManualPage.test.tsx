import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ManualPage } from '../src/pages/ManualPage';
import { useManual } from '../src/hooks/useApi';

vi.mock('../src/hooks/useApi', () => ({ useManual: vi.fn() }));

const manualResult = {
  data: {
    manual_id: 'project',
    title: '项目手册',
    content: '# 项目手册\n\n正文。',
    content_hash: 'a'.repeat(64),
    revision: 'b'.repeat(40),
  },
  isLoading: false,
  error: null,
  refetch: vi.fn(),
} as unknown as ReturnType<typeof useManual>;

describe('ManualPage', () => {
  beforeEach(() => {
    vi.mocked(useManual).mockReturnValue(manualResult);
  });

  it('renders the fixed manual and returns to the dashboard', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/manuals/project']}>
        <Routes>
          <Route path="/manuals/:manualId" element={<ManualPage />} />
          <Route path="/" element={<div>总览页</div>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole('heading', { name: '项目手册', level: 2 })).toBeInTheDocument();
    expect(screen.getByText('控制仓 revision')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '项目手册', level: 1 })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '← 返回总览' }));
    expect(await screen.findByText('总览页')).toBeInTheDocument();
  });
});
