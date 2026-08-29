import { Space, Typography } from 'antd';

interface PageHeaderProps {
  title: string;
  description?: string;
  extra?: React.ReactNode;
}

/** 页面标题区仅承载上下文和只读操作入口，不把未实现的控制动作伪装成可用按钮。 */
export function PageHeader({ title, description, extra }: PageHeaderProps) {
  return (
    <div className="page-header">
      <div>
        <Typography.Title level={2}>{title}</Typography.Title>
        {description && <Typography.Paragraph type="secondary">{description}</Typography.Paragraph>}
      </div>
      {extra && <Space>{extra}</Space>}
    </div>
  );
}
