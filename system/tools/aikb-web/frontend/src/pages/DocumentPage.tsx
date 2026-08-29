import { Alert, Button, Card, Col, Descriptions, Empty, Row, Space, Tag, Typography } from 'antd';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { AsyncState } from '../components/AsyncState';
import { MarkdownViewer } from '../components/MarkdownViewer';
import { PageHeader } from '../components/PageHeader';
import { useDocument } from '../hooks/useApi';
import { relationDirectionLabel, relationTypeLabel } from '../utils/relations';

/** Markdown 详情页只展示后端返回的 verified 文档，不提供编辑、删除或 Git 写入操作。 */
export function DocumentPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const id = params.get('id') ?? undefined;
  const query = useDocument(id);
  if (!id) return <><PageHeader title="知识阅读" /><Empty description="请从知识目录或搜索结果选择文档" /></>;
  const document = query.data;
  return (
    <>
      <PageHeader title={document?.title ?? '知识阅读'} description={document?.path} extra={<Button onClick={() => navigate('/knowledge')}>← 返回目录</Button>} />
      <AsyncState loading={query.isLoading} error={query.error} onRetry={() => void query.refetch()} empty={!document}>
        {document && <Row gutter={[16, 16]}>
          <Col xs={24} xl={17}><Card><MarkdownViewer content={document.content} /></Card></Col>
          <Col xs={24} xl={7}>
            <Card title="文档信息">
              <Descriptions column={1} size="small">
                <Descriptions.Item label="稳定 ID">{document.id}</Descriptions.Item>
                <Descriptions.Item label="逻辑路径">{document.path}</Descriptions.Item>
                <Descriptions.Item label="类型">{document.type}</Descriptions.Item>
                <Descriptions.Item label="状态"><Tag color="green">{document.status}</Tag></Descriptions.Item>
                <Descriptions.Item label="适用版本">{document.applicable_versions ?? '—'}</Descriptions.Item>
                <Descriptions.Item label="最近验证">{document.last_verified ?? '—'}</Descriptions.Item>
                <Descriptions.Item label="内容哈希"><Typography.Text copyable>{document.content_hash ?? '—'}</Typography.Text></Descriptions.Item>
              </Descriptions>
              <Typography.Paragraph type="secondary" className="metadata-label">标签</Typography.Paragraph>
              <Space wrap>{document.tags?.length ? document.tags.map((tag) => <Tag key={tag}>{tag}</Tag>) : <Typography.Text type="secondary">暂无标签</Typography.Text>}</Space>
            </Card>
            <Card title="关联知识" className="section-gap">
              {document.relations?.length ? document.relations.map((relation, index) => (
                <div className="related-item" key={`${relation.direction}-${relation.type}-${relation.target}-${index}`}>
                  <Link to={`/knowledge/view?id=${encodeURIComponent(relation.target)}`}>
                    {relation.target_title ?? '关联知识'}（稳定 ID：{relation.target}）
                  </Link>
                  <Typography.Text type="secondary">{relationDirectionLabel(relation.direction)} · {relationTypeLabel(relation.type)}</Typography.Text>
                </div>
              )) : <Typography.Text type="secondary">暂无关联知识</Typography.Text>}
            </Card>
            {document.truncated && <Alert className="section-gap" type="warning" showIcon message="正文超过第一阶段单次读取上限，当前展示内容已截断" />}
            <Alert className="section-gap" type="info" showIcon message="只读内容" description="此页面不会修改或删除正式知识，也不会直接访问本地文件。" />
          </Col>
        </Row>}
      </AsyncState>
    </>
  );
}
