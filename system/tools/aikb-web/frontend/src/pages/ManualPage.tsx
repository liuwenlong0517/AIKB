import { Alert, Button, Card, Descriptions, Empty, Typography } from 'antd';
import { useNavigate, useParams } from 'react-router-dom';
import { AsyncState } from '../components/AsyncState';
import { MarkdownViewer } from '../components/MarkdownViewer';
import { PageHeader } from '../components/PageHeader';
import { useManual } from '../hooks/useApi';

/**
 * 控制仓人类手册的独立阅读页；它不加入侧栏，返回总览后仍可从固定入口再次打开。
 * 正文、revision 和哈希均来自服务端白名单投影，页面不读取或推导本地路径。
 */
export function ManualPage() {
  const { manualId } = useParams();
  const navigate = useNavigate();
  const query = useManual(manualId);
  const manual = query.data;

  if (!manualId) return <><PageHeader title="使用指南" /><Empty description="请选择一份手册" /></>;
  return (
    <>
      <PageHeader
        title={manual?.title ?? '使用指南'}
        description="控制仓维护手册（只读）"
        extra={<Button onClick={() => navigate('/')}>← 返回总览</Button>}
      />
      <AsyncState loading={query.isLoading} error={query.error} onRetry={() => void query.refetch()} empty={!manual}>
        {manual && (
          <Card>
            <Descriptions column={2} size="small" className="manual-meta">
              <Descriptions.Item label="逻辑标识">{manual.manual_id}</Descriptions.Item>
              <Descriptions.Item label="控制仓 revision">{manual.revision}</Descriptions.Item>
              <Descriptions.Item label="内容哈希" span={2}><Typography.Text copyable>{manual.content_hash}</Typography.Text></Descriptions.Item>
            </Descriptions>
            {manual.truncated && <Alert className="section-gap" type="warning" showIcon message="正文超过单次读取上限，当前展示内容已截断" />}
            <MarkdownViewer content={manual.content} />
          </Card>
        )}
      </AsyncState>
    </>
  );
}
