import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AppLayout } from '../components/AppLayout';
import { DashboardPage } from '../pages/DashboardPage';
import { DocumentPage } from '../pages/DocumentPage';
import { KnowledgePage } from '../pages/KnowledgePage';
import { SearchPage } from '../pages/SearchPage';
import { SystemPage } from '../pages/SystemPage';
import { RuntimePage } from '../pages/RuntimePage';
import { AuditPage } from '../pages/AuditPage';
import { TasksPage } from '../pages/TasksPage';
import { RulesPage } from '../pages/RulesPage';
import { MaintenancePage } from '../pages/MaintenancePage';
import { ManualPage } from '../pages/ManualPage';

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: 1, refetchOnWindowFocus: false } },
});

/** 应用路由边界。所有页面共用同一个查询缓存，避免切换只读页面时重复请求。 */
export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/manuals/:manualId" element={<ManualPage />} />
            <Route path="/knowledge" element={<KnowledgePage />} />
            <Route path="/knowledge/view" element={<DocumentPage />} />
            <Route path="/rules" element={<RulesPage />} />
            <Route path="/rules/:ruleId" element={<RulesPage />} />
            <Route path="/maintenance" element={<MaintenancePage />} />
            <Route path="/search" element={<SearchPage />} />
            <Route path="/system" element={<SystemPage />} />
            <Route path="/runtime" element={<RuntimePage />} />
            <Route path="/runtime/:workId" element={<RuntimePage />} />
            <Route path="/runtime/:workId/checkpoints/:checkpointId" element={<RuntimePage />} />
            <Route path="/runtime/history" element={<RuntimePage />} />
            <Route path="/runtime/history/:historyWorkId" element={<RuntimePage />} />
            <Route path="/runtime/history/:historyWorkId/checkpoints/:historyCheckpointId" element={<RuntimePage />} />
            <Route path="/audit" element={<AuditPage />} />
            <Route path="/audit/:invocationId" element={<AuditPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/tasks/:taskId" element={<TasksPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
