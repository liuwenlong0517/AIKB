import { Alert, Card, Col, Descriptions, List, Row, Tag, Typography } from 'antd';
import { AsyncState } from '../components/AsyncState';
import { PageHeader } from '../components/PageHeader';
import { useSystem } from '../hooks/useApi';

/** 系统页合并平台能力、双仓 Git 摘要和索引状态，不提供任何修复动作。 */
export function SystemPage() {
  const query = useSystem();
  const data = query.data;
  const platform = data?.capabilities.platform;
  const repositories = data ? Object.entries(data.info.repositories) : [];
  return (
    <>
      <PageHeader title="系统状态" description="查看 API、索引、平台和仓库的只读状态。" />
      <AsyncState loading={query.isLoading} error={query.error} onRetry={() => void query.refetch()} empty={!data}>
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={12}>
            <Card title="平台能力"><Descriptions column={1}>
              <Descriptions.Item label="平台">{platform?.platform ?? data?.info.platform.name ?? '未知'}</Descriptions.Item>
              <Descriptions.Item label="架构">{data?.info.platform.architecture ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="状态">{platform?.supported ? <Tag color="green">已支持</Tag> : <Tag color="orange">{platform?.reason ?? '暂未支持'}</Tag>}</Descriptions.Item>
              <Descriptions.Item label="模式"><Tag color="blue">{data?.capabilities.read_only ? '只读' : '未知'}</Tag></Descriptions.Item>
              <Descriptions.Item label="Python">{data?.info.python.version ?? '—'}</Descriptions.Item>
            </Descriptions></Card>
          </Col>
          <Col xs={24} lg={12}>
            <Card title="派生索引"><Descriptions column={1}>
              <Descriptions.Item label="状态">{data?.info.index.tokenizer ? <Tag color="green">可用</Tag> : <Tag>未知</Tag>}</Descriptions.Item>
              <Descriptions.Item label="Tokenizer">{data?.info.index.tokenizer ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="本次重建">{data?.info.index.rebuilt ? '是' : '否'}</Descriptions.Item>
            </Descriptions></Card>
          </Col>
          <Col xs={24}><Card title="仓库状态"><List dataSource={repositories} locale={{ emptyText: '暂无仓库状态' }} renderItem={([name, repo]) => <List.Item><div><Typography.Text strong>{name === 'control' ? '控制仓' : '知识仓'}</Typography.Text><Typography.Paragraph type="secondary">{repo.branch ?? '未知分支'} · {repo.short_commit ?? '无提交信息'}</Typography.Paragraph></div><Tag color={repo.available ? 'green' : 'orange'}>{repo.available ? '可用' : '不可用'}</Tag></List.Item>} /></Card></Col>
          <Col xs={24}><Card title="第一阶段能力"><List dataSource={data?.capabilities.capabilities ?? []} renderItem={(capability) => <List.Item><span>{capability.id}</span>{capability.supported ? <Tag color="green">可用</Tag> : <span><Tag>不可用</Tag>{capability.reason && <Typography.Text type="secondary">{capability.reason}</Typography.Text>}</span>}</List.Item>} /></Card></Col>
        </Row>
        {platform && !platform.supported && <Alert className="section-gap" type="warning" showIcon message="当前平台未提供可用实现" description={platform.reason} />}
      </AsyncState>
    </>
  );
}
