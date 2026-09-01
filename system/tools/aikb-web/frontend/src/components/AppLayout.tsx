import { FloatButton, Layout, Menu, Tag, Typography } from 'antd';
import { Link, Outlet, useLocation } from 'react-router-dom';

const { Header, Sider, Content } = Layout;

const navigation = [
  { key: '/', label: <Link to="/">总览</Link> },
  { key: '/search', label: <Link to="/search">搜索</Link> },
  { key: '/knowledge', label: <Link to="/knowledge">知识库</Link> },
  { key: '/rules', label: <Link to="/rules">规则中心</Link> },
  { key: '/maintenance', label: <Link to="/maintenance">安装与修复</Link> },
  { key: '/runtime', label: <Link to="/runtime">运行状态</Link> },
  { key: '/audit', label: <Link to="/audit">审计日志</Link> },
  { key: '/tasks', label: <Link to="/tasks">任务中心</Link> },
  { key: '/system', label: <Link to="/system">系统状态</Link> },
];

/** 回顶组件必须监听右侧内容区；在测试或首屏挂载尚未完成时才回退到窗口。 */
const getScrollContainer = () => document.getElementById('app-scroll-container') ?? window;

/** 桌面管理布局。导航只列出已实现的读取页面与受控任务中心。 */
export function AppLayout() {
  const location = useLocation();
  const selected = location.pathname.startsWith('/knowledge')
    ? '/knowledge'
    : location.pathname.startsWith('/rules')
      ? '/rules'
    : location.pathname.startsWith('/maintenance')
      ? '/maintenance'
    : location.pathname.startsWith('/search')
      ? '/search'
      : location.pathname.startsWith('/runtime')
        ? '/runtime'
        : location.pathname.startsWith('/audit')
          ? '/audit'
          : location.pathname.startsWith('/tasks')
            ? '/tasks'
      : location.pathname.startsWith('/system')
        ? '/system'
        : '/';

  return (
    <Layout className="app-shell">
      <Sider width={200} className="app-sider">
        <div className="brand-mark"><span>AI</span>KB</div>
        <Typography.Text className="sider-caption">本地知识管理终端</Typography.Text>
        <Menu theme="dark" mode="inline" selectedKeys={[selected]} items={navigation} />
      </Sider>
      <Layout className="app-main">
        <Header className="app-header">
          <div>
            <Typography.Text strong>AIKB 知识管理</Typography.Text>
            <Typography.Text type="secondary" className="header-context">本地受控管理模式</Typography.Text>
          </div>
          <Tag color="green">本地</Tag>
        </Header>
        <Content id="app-scroll-container" className="app-scroll-container">
          <div className="app-content"><Outlet /></div>
        </Content>
      </Layout>
      {/* 回到右侧内容区顶部，避免把不会滚动的侧栏或整个窗口当作目标。 */}
      <FloatButton.BackTop
        aria-label="回到页首"
        target={getScrollContainer}
        visibilityHeight={300}
        tooltip="回到页首"
      />
    </Layout>
  );
}
