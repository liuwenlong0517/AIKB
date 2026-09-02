import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { lazy, Suspense } from 'react';
import type { ComponentType } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AppLayout } from '../components/AppLayout';

const DashboardPage = lazy(() => import('../pages/DashboardPage').then(({ DashboardPage: page }) => ({ default: page })));
const DocumentPage = lazy(() => import('../pages/DocumentPage').then(({ DocumentPage: page }) => ({ default: page })));
const KnowledgePage = lazy(() => import('../pages/KnowledgePage').then(({ KnowledgePage: page }) => ({ default: page })));
const SearchPage = lazy(() => import('../pages/SearchPage').then(({ SearchPage: page }) => ({ default: page })));
const SystemPage = lazy(() => import('../pages/SystemPage').then(({ SystemPage: page }) => ({ default: page })));
const RuntimePage = lazy(() => import('../pages/RuntimePage').then(({ RuntimePage: page }) => ({ default: page })));
const AuditPage = lazy(() => import('../pages/AuditPage').then(({ AuditPage: page }) => ({ default: page })));
const TasksPage = lazy(() => import('../pages/TasksPage').then(({ TasksPage: page }) => ({ default: page })));
const RulesPage = lazy(() => import('../pages/RulesPage').then(({ RulesPage: page }) => ({ default: page })));
const MaintenancePage = lazy(() => import('../pages/MaintenancePage').then(({ MaintenancePage: page }) => ({ default: page })));
const ManualPage = lazy(() => import('../pages/ManualPage').then(({ ManualPage: page }) => ({ default: page })));
const DataMaintenancePage = lazy(() => import('../pages/DataMaintenancePage').then(({ DataMaintenancePage: page }) => ({ default: page })));

/** 保留布局壳层常驻，仅替换当前页面内容，避免每次懒加载都闪烁整个应用框架。 */
function LazyRoute({ page: Page }: { page: ComponentType }) {
  return <Suspense fallback={<div className="app-content" role="status">正在加载页面…</div>}><Page /></Suspense>;
}

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: 1, refetchOnWindowFocus: false } },
});

/** 应用路由边界。所有页面共用同一个查询缓存，页面正文按路由懒加载以缩短首屏脚本。 */
export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="/" element={<LazyRoute page={DashboardPage} />} />
            <Route path="/manuals/:manualId" element={<LazyRoute page={ManualPage} />} />
            <Route path="/knowledge" element={<LazyRoute page={KnowledgePage} />} />
            <Route path="/knowledge/view" element={<LazyRoute page={DocumentPage} />} />
            <Route path="/rules" element={<LazyRoute page={RulesPage} />} />
            <Route path="/rules/:ruleId" element={<LazyRoute page={RulesPage} />} />
            <Route path="/maintenance" element={<LazyRoute page={MaintenancePage} />} />
            <Route path="/data-maintenance" element={<LazyRoute page={DataMaintenancePage} />} />
            <Route path="/search" element={<LazyRoute page={SearchPage} />} />
            <Route path="/system" element={<LazyRoute page={SystemPage} />} />
            <Route path="/runtime" element={<LazyRoute page={RuntimePage} />} />
            <Route path="/runtime/:workId" element={<LazyRoute page={RuntimePage} />} />
            <Route path="/runtime/:workId/checkpoints/:checkpointId" element={<LazyRoute page={RuntimePage} />} />
            <Route path="/runtime/history" element={<LazyRoute page={RuntimePage} />} />
            <Route path="/runtime/history/:historyWorkId" element={<LazyRoute page={RuntimePage} />} />
            <Route path="/runtime/history/:historyWorkId/checkpoints/:historyCheckpointId" element={<LazyRoute page={RuntimePage} />} />
            <Route path="/audit" element={<LazyRoute page={AuditPage} />} />
            <Route path="/audit/:invocationId" element={<LazyRoute page={AuditPage} />} />
            <Route path="/tasks" element={<LazyRoute page={TasksPage} />} />
            <Route path="/tasks/:taskId" element={<LazyRoute page={TasksPage} />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
