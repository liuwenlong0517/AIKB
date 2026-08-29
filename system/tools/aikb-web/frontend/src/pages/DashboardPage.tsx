import { Alert, Card, Col, List, Row, Statistic, Tag, Typography } from 'antd';
import { Link } from 'react-router-dom';
import { AsyncState } from '../components/AsyncState';
import { PageHeader } from '../components/PageHeader';
import { useOverview } from '../hooks/useApi';

/** 总览页只呈现共享核心返回的 verified 统计，不推测检查点或审计状态。 */
export function DashboardPage() {
  const query = useOverview();
  const data = query.data;
  const recentDate = data?.recent_documents.find((item) => item.last_verified)?.last_verified;
  return (
    <>
      <PageHeader title="总览" description="查看 AIKB 正式知识、索引和最近验证状态。" />
      <AsyncState loading={query.isLoading} error={query.error} onRetry={() => void query.refetch()} empty={!data}>
        <Row gutter={[16, 16]}>
          <Col xs={24} sm={12} lg={6}><Card><Statistic title="正式知识" value={data?.document_count ?? 0} suffix="篇" /></Card></Col>
          <Col xs={24} sm={12} lg={6}><Card><Statistic title="知识类型" value={Object.keys(data?.by_type ?? {}).length} suffix="类" /></Card></Col>
          <Col xs={24} sm={12} lg={6}><Card><Statistic title="标签" value={data?.by_tag.length ?? 0} suffix="个" /></Card></Col>
          <Col xs={24} sm={12} lg={6}><Card><Statistic title="索引" value={data?.index?.tokenizer ?? '未知'} /></Card></Col>
        </Row>
        {data?.index?.rebuilt && <Alert className="section-gap" type="info" showIcon message="本次访问已根据 Markdown 事实源重建派生索引" />}
        <Row gutter={[16, 16]} className="section-gap">
          <Col xs={24} lg={12}>
            <Card title="知识类型分布">
              <List dataSource={Object.entries(data?.by_type ?? {})} locale={{ emptyText: '暂无类型统计' }} renderItem={([name, count]) => <List.Item><span>{name}</span><Tag>{count} 篇</Tag></List.Item>} />
            </Card>
          </Col>
          <Col xs={24} lg={12}>
            <Card title="最近验证" extra={<Typography.Text type="secondary">{formatDate(recentDate)}</Typography.Text>}>
              <List dataSource={data?.recent_documents ?? []} locale={{ emptyText: '暂无最近文档' }} renderItem={(item) => (
                <List.Item>
                  <div><Link to={`/knowledge/view?id=${encodeURIComponent(item.id)}`}>{item.title}</Link><Typography.Paragraph type="secondary" ellipsis={{ rows: 1 }}>{item.path}</Typography.Paragraph></div>
                  <Typography.Text type="secondary">{formatDate(item.last_verified)}</Typography.Text>
                </List.Item>
              )} />
            </Card>
          </Col>
        </Row>
      </AsyncState>
    </>
  );
}

function formatDate(value?: string | null) { return value ? new Date(`${value}T00:00:00`).toLocaleDateString('zh-CN') : '—'; }
