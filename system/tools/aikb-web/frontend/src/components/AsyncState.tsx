import { Alert, Button, Empty, Spin } from 'antd';

interface AsyncStateProps {
  loading: boolean;
  error: Error | null;
  empty?: boolean;
  emptyDescription?: string;
  onRetry?: () => void;
  children: React.ReactNode;
}

/** 页面统一的加载、错误和空结果状态，确保后端不可用时用户得到可行动的说明。 */
export function AsyncState({ loading, error, empty, emptyDescription, onRetry, children }: AsyncStateProps) {
  if (loading) return <div className="state-panel"><Spin size="large" tip="正在读取 AIKB 数据…" /></div>;
  if (error) {
    return (
      <div className="state-panel">
        <Alert
          type="error"
          showIcon
          message="暂时无法读取数据"
          description={error.message}
          action={onRetry ? <Button onClick={onRetry}>重试</Button> : undefined}
        />
      </div>
    );
  }
  if (empty) return <Empty description={emptyDescription ?? '暂无数据'} className="state-panel" />;
  return <>{children}</>;
}
