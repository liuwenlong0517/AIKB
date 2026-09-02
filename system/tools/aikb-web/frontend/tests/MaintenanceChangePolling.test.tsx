import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useMaintenanceChange } from '../src/hooks/useApi';

const mocks = vi.hoisted(() => ({ change: vi.fn() }));
vi.mock('../src/api/client', () => ({ api: { maintenance: { change: mocks.change } } }));

function Probe() {
  const query = useMaintenanceChange('change-1');
  return <output data-testid="status">{query.data?.data.change.status ?? ''}</output>;
}

describe('maintenance change polling', () => {
  it('expired 事务停止轮询', async () => {
    mocks.change.mockResolvedValue({ data: { change: { change_id: 'change-1', status: 'expired' } }, meta: {} });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><Probe /></QueryClientProvider>);
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('expired'));
    await new Promise((resolve) => setTimeout(resolve, 2_200));
    expect(mocks.change).toHaveBeenCalledTimes(1);
  });
});
