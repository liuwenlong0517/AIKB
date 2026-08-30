import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { SystemPage } from '../src/pages/SystemPage';

const useSystem = vi.hoisted(() => vi.fn());
vi.mock('../src/hooks/useApi', () => ({ useSystem }));

describe('SystemPage 规则恢复提示', () => {
  it('展示后端提供的规则写入阻断和人工恢复摘要', () => {
    useSystem.mockReturnValue({
      data: {
        info: {
          platform: { name: 'windows', architecture: 'amd64' },
          python: { version: '3.11' },
          repositories: { control: { available: true }, knowledge: { available: true } },
          index: {},
          rule_writes: { available: true, blocked: true, recovery_required: true, warning: 'rule_recovery_required' },
        },
        capabilities: { platform: { platform: 'windows', supported: true }, read_only: true, capabilities: [] },
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    render(<MemoryRouter><SystemPage /></MemoryRouter>);
    expect(screen.getByText('规则写入状态')).toBeInTheDocument();
    expect(screen.getByText('已阻断，需人工恢复')).toBeInTheDocument();
    expect(screen.getByText('规则写入需要人工恢复')).toBeInTheDocument();
    expect(screen.getByText('rule_recovery_required')).toBeInTheDocument();
  });
});
