import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../src/app/App';

vi.mock('../src/pages/SearchPage', () => ({ SearchPage: () => <div>懒加载搜索页面</div> }));

describe('App 路由懒加载', () => {
  afterEach(() => window.history.replaceState({}, '', '/'));

  it('进入路由时先显示加载状态，页面模块加载后正常渲染', async () => {
    window.history.replaceState({}, '', '/search');
    render(<App />);
    expect(await screen.findByText('懒加载搜索页面')).toBeInTheDocument();
  });
});
