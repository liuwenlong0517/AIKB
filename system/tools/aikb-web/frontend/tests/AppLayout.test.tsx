import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom';
import { AppLayout } from '../src/components/AppLayout';

describe('AppLayout', () => {
  it('保留侧栏导航并将嵌套路由渲染到右侧内容区', async () => {
    render(
      <MemoryRouter initialEntries={['/knowledge/view']}>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="/knowledge/view" element={<div>测试知识文档</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole('link', { name: '总览' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '知识库' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '搜索' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '系统状态' })).toBeInTheDocument();
    expect(screen.getByText('测试知识文档')).toBeInTheDocument();
    expect(document.getElementById('app-scroll-container')).toBeInTheDocument();
    // rc-menu 会在挂载后异步同步选中项，等待该更新完成，避免测试结束时遗留 act 警告。
    await waitFor(() => expect(screen.getByRole('link', { name: '知识库' })).toBeInTheDocument());
  });

  it('挂载绑定到右侧滚动容器的回到页首控件', async () => {
    render(
      <MemoryRouter>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="*" element={<Outlet />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    const container = document.getElementById('app-scroll-container');
    expect(container).toBeInTheDocument();
    if (!container) throw new Error('右侧滚动容器未挂载');

    // Ant Design 在 jsdom 中会按滚动阈值隐藏 BackTop，模拟右侧容器滚动后再验证其结构。
    Object.defineProperty(container, 'scrollTop', { configurable: true, value: 400, writable: true });
    fireEvent.scroll(container);
    const backTop = await waitFor(() => {
      const element = document.querySelector('.ant-float-btn');
      expect(element).toBeInTheDocument();
      return element;
    });
    expect(backTop).toBeInTheDocument();
    expect(backTop).toBeInstanceOf(HTMLButtonElement);
    expect(backTop).toHaveClass('ant-float-btn');
  });
});
