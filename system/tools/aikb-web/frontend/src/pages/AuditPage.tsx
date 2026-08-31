import { Alert, Button, Card, Col, Descriptions, Empty, Input, List, Pagination as AntPagination, Row, Select, Space, Statistic, Tag, Typography } from 'antd';
import { Link, useParams } from 'react-router-dom';
import { useState } from 'react';
import { AsyncState } from '../components/AsyncState';
import { PageHeader } from '../components/PageHeader';
import { useAuditEvent, useAuditEvents, useAuditSummary } from '../hooks/useApi';
import type { ApiMeta, AuditEvent, AuditStatus } from '../types/api';

const STATUS_LABELS: Record<AuditStatus, string> = { started: '已开始', succeeded: '成功', failed: '失败', noop: '无操作', blocked: '已阻止', incomplete: '未完成', cancelled: '已取消', timed_out: '已超时', interrupted: '已中断' };
const STATUS_COLORS: Record<AuditStatus, string> = { started: 'blue', succeeded: 'green', failed: 'red', noop: 'default', blocked: 'orange', incomplete: 'gold', cancelled: 'default', timed_out: 'orange', interrupted: 'purple' };
const OPERATION_LABELS: Record<string, string> = {
  claim_work_state: '显式认领旧工作状态',
  authorize_work_participant: 'Owner 管理参与会话（授权/交接/撤销）',
};

/** 审计降级提示只映射固定 warning 枚举，避免把 JSONL 路径或异常正文带到浏览器。 */
function WarningBar({ meta }: { meta?: ApiMeta }) {
  const labels: Record<string, string> = { audit_partial: '审计记录部分不可读。', damaged_records: '存在损坏记录，损坏详情仅保留计数。', session_id_unavailable: '部分记录未提供真实会话 ID。' };
  const warnings = (meta?.warnings ?? []).map((warning) => labels[warning] ?? '部分审计数据处于降级状态。');
  if (!warnings.length && !meta?.degraded) return null;
  return <Alert className="section-gap" type="warning" showIcon message="审计数据部分降级" description={[...new Set(warnings)].join(' ') || '部分数据不可用，但当前安全子集仍可查看。'} />;
}

/** 合并独立摘要与列表响应的可信度元数据，任一接口降级都必须在页面保留提示。 */
function mergeMeta(...metas: Array<ApiMeta | undefined>): ApiMeta | undefined {
  const available = metas.filter((meta): meta is ApiMeta => Boolean(meta));
  if (!available.length) return undefined;
  return {
    ...available[0],
    degraded: available.some((meta) => meta.degraded === true),
    warnings: [...new Set(available.flatMap((meta) => meta.warnings ?? []))],
  };
}

function statusTag(status?: string | null) { const typed = status as AuditStatus; return <Tag color={STATUS_COLORS[typed] ?? 'default'}>{STATUS_LABELS[typed] ?? status ?? '未知状态'}</Tag>; }
function value(input: unknown): string { return input === undefined || input === null || input === '' ? '—' : String(input); }
/** 对归属治理事件使用固定中文解释；未知 operation 仍保留安全原值便于审计。 */
function operationLabel(operation?: string | null): string { return OPERATION_LABELS[operation ?? ''] ?? value(operation); }
function session(valueInput?: string | null): string { return valueInput ?? '未提供会话 ID'; }
function date(input?: string | null): string { if (!input) return '—'; const parsed = new Date(input); return Number.isNaN(parsed.getTime()) ? input : parsed.toLocaleString('zh-CN'); }
/** 普通调用使用 invocation_id；连接初始化等独立事件安全回退到 event_id。 */
function auditIdentifier(item: AuditEvent): string | null { return item.invocation_id ?? item.event_id ?? null; }

/** 审计页面列表和调用详情共用路由，保证 /audit/:invocationId 刷新时仍是可读深链。 */
export function AuditPage() {
  const { invocationId } = useParams<{ invocationId?: string }>();
  return invocationId ? <AuditDetail invocationId={invocationId} /> : <AuditList />;
}

function AuditList() {
  const [since, setSince] = useState('');
  const [dateFilter, setDateFilter] = useState('');
  const [agent, setAgent] = useState('');
  const [source, setSource] = useState<string>();
  const [status, setStatus] = useState<string>();
  const [operation, setOperation] = useState('');
  const [page, setPage] = useState(1);
  const pageSize = 50;
  const common = { since: since || undefined, date: dateFilter || undefined, agent: agent || undefined, source, status, operation, page, page_size: pageSize };
  // summary 与 events 使用同一组公共筛选，顶部数字始终对应当前列表语义，而不是全量审计数据。
  const summaryQuery = useAuditSummary({ since: since || undefined, date: dateFilter || undefined, agent: agent || undefined, source, status, operation: operation || undefined });
  const eventsQuery = useAuditEvents(common);
  const summary = summaryQuery.data?.data;
  const events = eventsQuery.data?.data;
  const clear = () => { setSince(''); setDateFilter(''); setAgent(''); setSource(undefined); setStatus(undefined); setOperation(''); setPage(1); };
  return <>
    <PageHeader title="审计日志" description="查看本机 MCP、hook 与 Web 受控动作的安全摘要和有限详情。" />
    <WarningBar meta={mergeMeta(summaryQuery.data?.meta, eventsQuery.data?.meta)} />
    <AsyncState loading={summaryQuery.isLoading} error={summaryQuery.error} onRetry={() => void summaryQuery.refetch()} empty={!summary}>
      {summary && <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}><Card><Statistic title="调用总数" value={summary.count} /></Card></Col>
        <Col xs={12} sm={6}><Card><Statistic title="成功" value={summary.statuses.succeeded ?? 0} /></Card></Col>
        <Col xs={12} sm={6}><Card><Statistic title="失败 / 未完成" value={(summary.statuses.failed ?? 0) + (summary.statuses.incomplete ?? 0)} /></Card></Col>
        <Col xs={12} sm={6}><Card><Statistic title="平均耗时" value={summary.average_duration_ms ?? 0} suffix="ms" /></Card></Col>
        <Col xs={24} lg={12}><Card title="状态分布"><Space wrap>{Object.entries(summary.statuses).map(([key, count]) => <Tag key={key}>{STATUS_LABELS[key as AuditStatus] ?? key}：{count}</Tag>)}</Space></Card></Col>
        <Col xs={24} lg={12}><Card title="来源与降级"><Space wrap><Tag>mcp：{summary.sources.mcp ?? 0}</Tag><Tag>hook：{summary.sources.hook ?? 0}</Tag><Tag>web：{summary.sources.web ?? 0}</Tag><Tag color={summary.fallback_records ? 'orange' : undefined}>fallback：{summary.fallback_records ?? 0}</Tag><Tag color={summary.damaged_count ? 'red' : undefined}>损坏：{summary.damaged_count ?? 0}</Tag></Space><Typography.Paragraph type="secondary">最近活动：{date(summary.last_activity)}</Typography.Paragraph></Card></Col>
      </Row>}
    </AsyncState>
    <Card className="section-gap search-controls">
      <Space wrap>
        <Input aria-label="审计时间范围" placeholder="since，例如 24h 或 7d" value={since} onChange={(event) => { setSince(event.target.value); setDateFilter(''); setPage(1); }} />
        <Input aria-label="审计日期" placeholder="日期，例如 2026-08-29" value={dateFilter} onChange={(event) => { setDateFilter(event.target.value); setSince(''); setPage(1); }} />
        <Input aria-label="按 Agent 筛选审计" placeholder="Agent" value={agent} onChange={(event) => { setAgent(event.target.value); setPage(1); }} />
        <Input aria-label="按操作筛选审计" placeholder="操作" value={operation} onChange={(event) => { setOperation(event.target.value); setPage(1); }} />
        <Select aria-label="按来源筛选审计" allowClear placeholder="全部来源" style={{ width: 130 }} value={source} onChange={(value) => { setSource(value); setPage(1); }} options={[{ value: 'mcp', label: 'MCP' }, { value: 'hook', label: 'Hook' }, { value: 'web', label: 'Web' }]} />
        <Select aria-label="按状态筛选审计" allowClear placeholder="全部状态" style={{ width: 150 }} value={status} onChange={(value) => { setStatus(value); setPage(1); }} options={Object.entries(STATUS_LABELS).map(([valueKey, label]) => ({ value: valueKey, label }))} />
        <Button onClick={clear}>清除筛选</Button>
      </Space>
    </Card>
    <AsyncState loading={eventsQuery.isLoading} error={eventsQuery.error} onRetry={() => void eventsQuery.refetch()} empty={!events} emptyDescription="没有匹配的审计调用。">
      {events && <Card title={<span>调用列表 <Typography.Text type="secondary">{events.pagination.total ?? events.items.length} 项</Typography.Text></span>}>
        {events.items.length === 0 ? <Empty description="没有匹配的审计调用。" /> : <List itemLayout="vertical" dataSource={events.items} renderItem={(item) => {
          const identifier = auditIdentifier(item);
          const title = <Space>{identifier ? <Link to={`/audit/${encodeURIComponent(identifier)}`}>{operationLabel(item.operation)}</Link> : <Typography.Text>{operationLabel(item.operation)}</Typography.Text>}{statusTag(item.status)}</Space>;
          return <List.Item actions={identifier ? [<Link key="detail" to={`/audit/${encodeURIComponent(identifier)}`}>查看详情</Link>] : []}><List.Item.Meta title={title} description={<Space wrap><Typography.Text type="secondary">会话：{item.session_label ?? '未提供会话标签'}</Typography.Text><Typography.Text type="secondary">Agent：{value(item.agent)}</Typography.Text><Typography.Text type="secondary">来源：{value(item.source)}</Typography.Text><Typography.Text type="secondary">开始：{date(item.started_at)}</Typography.Text></Space>} /><Typography.Paragraph ellipsis={{ rows: 2 }}>{value(item.action_text)}{item.result_text ? ` · ${item.result_text}` : ''}</Typography.Paragraph><Space wrap><Typography.Text type="secondary">耗时：{item.duration_ms == null ? '—' : `${item.duration_ms} ms`}</Typography.Text>{item.fallback && <Tag color="orange">fallback</Tag>}<Typography.Text type="secondary">会话 ID：{session(item.session_id)}</Typography.Text></Space></List.Item>;
        }} />}
        {events.pagination.total !== null && events.pagination.total > pageSize && <AntPagination className="section-gap" current={page} pageSize={pageSize} total={events.pagination.total} showSizeChanger={false} onChange={setPage} />}
      </Card>}
    </AsyncState>
  </>;
}

function AuditDetail({ invocationId }: { invocationId: string }) {
  const query = useAuditEvent(invocationId);
  const item = query.data?.data;
  return <>
    <PageHeader title="审计调用详情" description="有限安全投影；不展示原始 action、result 或诊断附件。" extra={<Button href="/audit">← 返回审计日志</Button>} />
    <WarningBar meta={query.data?.meta} />
    <AsyncState loading={query.isLoading} error={query.error} onRetry={() => void query.refetch()} empty={!item} emptyDescription="未找到该审计调用。">
      {item && <AuditEventCard item={item} />}
    </AsyncState>
  </>;
}

function AuditEventCard({ item }: { item: AuditEvent }) {
  return <Card title={<Space>{operationLabel(item.operation)} {statusTag(item.status)}</Space>}><Descriptions column={1} size="small"><Descriptions.Item label="调用 ID">{value(item.invocation_id)}</Descriptions.Item><Descriptions.Item label="事件 ID">{value(item.event_id)}</Descriptions.Item><Descriptions.Item label="开始时间">{date(item.started_at)}</Descriptions.Item><Descriptions.Item label="结束时间">{date(item.finished_at)}</Descriptions.Item><Descriptions.Item label="来源 / Agent">{value(item.source)} / {value(item.agent)}</Descriptions.Item><Descriptions.Item label="会话标签">{item.session_label ?? '未提供会话标签'}</Descriptions.Item><Descriptions.Item label="会话 ID">{session(item.session_id)}</Descriptions.Item><Descriptions.Item label="项目逻辑 ID">{value(item.project_id)}</Descriptions.Item><Descriptions.Item label="动作说明">{value(item.action_text)}</Descriptions.Item><Descriptions.Item label="结果说明">{value(item.result_text)}</Descriptions.Item><Descriptions.Item label="结果代码">{value(item.outcome_code)}</Descriptions.Item><Descriptions.Item label="错误类型">{value(item.error_type)}</Descriptions.Item><Descriptions.Item label="捕获级别">{value(item.capture_level)}</Descriptions.Item><Descriptions.Item label="耗时">{item.duration_ms == null ? '—' : `${item.duration_ms} ms`}</Descriptions.Item><Descriptions.Item label="降级记录">{item.fallback ? <Tag color="orange">是</Tag> : '否'}</Descriptions.Item></Descriptions></Card>;
}
