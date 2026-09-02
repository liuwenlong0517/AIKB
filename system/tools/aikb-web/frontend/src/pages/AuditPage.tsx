import { Alert, Button, Card, Col, Descriptions, Empty, Input, List, Pagination as AntPagination, Row, Select, Space, Statistic, Tag, Typography } from 'antd';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AsyncState } from '../components/AsyncState';
import { PageHeader } from '../components/PageHeader';
import { useAuditEvent, useAuditEvents } from '../hooks/useApi';
import type { ApiMeta, AuditEvent, AuditStatus } from '../types/api';
import { useDebouncedValue } from '../hooks/useDebouncedValue';

const STATUS_LABELS: Record<AuditStatus, string> = { started: '已开始', succeeded: '成功', failed: '失败', noop: '无操作', blocked: '已阻止', incomplete: '未完成', cancelled: '已取消', timed_out: '已超时', interrupted: '已中断' };
const STATUS_COLORS: Record<AuditStatus, string> = { started: 'blue', succeeded: 'green', failed: 'red', noop: 'default', blocked: 'orange', incomplete: 'gold', cancelled: 'default', timed_out: 'orange', interrupted: 'purple' };
const OPERATION_LABELS: Record<string, string> = {
  claim_work_state: '显式认领旧工作状态',
  authorize_work_participant: 'Owner 管理参与会话（授权/交接/撤销）',
};
function pageNumber(value: string | null): number { const parsed = Number(value); return Number.isInteger(parsed) && parsed > 0 ? parsed : 1; }

/** 审计降级提示只映射固定 warning 枚举，避免把 JSONL 路径或异常正文带到浏览器。 */
function WarningBar({ meta }: { meta?: ApiMeta }) {
  const labels: Record<string, string> = { audit_partial: '审计记录部分不可读。', damaged_records: '存在损坏记录，损坏详情仅保留计数。', session_id_unavailable: '部分记录未提供真实会话 ID。' };
  const warnings = (meta?.warnings ?? []).map((warning) => labels[warning] ?? '部分审计数据处于降级状态。');
  if (!warnings.length && !meta?.degraded) return null;
  return <Alert className="section-gap" type="warning" showIcon message="审计数据部分降级" description={[...new Set(warnings)].join(' ') || '部分数据不可用，但当前安全子集仍可查看。'} />;
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
  const pageSize = 50;
  const [searchParams, setSearchParams] = useSearchParams();
  const urlString = searchParams.toString();
  const urlSince = searchParams.get('since') ?? '';
  const urlDate = searchParams.get('date') ?? '';
  const urlAgent = searchParams.get('agent') ?? '';
  const urlOperation = searchParams.get('operation') ?? '';
  const urlSource = searchParams.get('source') || undefined;
  const urlStatus = searchParams.get('status') || undefined;
  const urlPage = pageNumber(searchParams.get('page'));
  const [since, setSince] = useState(urlSince);
  const [dateFilter, setDateFilter] = useState(urlDate);
  const [agent, setAgent] = useState(urlAgent);
  const [source, setSource] = useState<string | undefined>(urlSource);
  const [status, setStatus] = useState<string | undefined>(urlStatus);
  const [operation, setOperation] = useState(urlOperation);
  const [page, setPage] = useState(urlPage);
  const urlSyncRef = useRef(false);
  const draftRef = useRef({ since: urlSince, date: urlDate, agent: urlAgent, operation: urlOperation });
  const searchParamsRef = useRef(searchParams);
  searchParamsRef.current = searchParams;
  const textDraft = useMemo(() => ({ since, dateFilter, agent, operation }), [since, dateFilter, agent, operation]);
  const debouncedText = useDebouncedValue(textDraft);
  useEffect(() => {
    const textChanged = urlSince !== since || urlDate !== dateFilter || urlAgent !== agent || urlOperation !== operation;
    if (textChanged) urlSyncRef.current = true;
    draftRef.current = { since: urlSince, date: urlDate, agent: urlAgent, operation: urlOperation };
    setSince(urlSince); setDateFilter(urlDate); setAgent(urlAgent); setOperation(urlOperation);
    setSource(urlSource); setStatus(urlStatus); setPage(urlPage);
  // URL 字符串是后退/前进恢复的唯一同步边界；状态选择器变化不会覆盖正在编辑的文本。
  // 仅监听 URL，若监听草稿状态会把每次输入误判成浏览器导航并跳过防抖提交。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlString]);
  useEffect(() => {
    if (urlSyncRef.current) { urlSyncRef.current = false; return; }
    if (debouncedText.since === urlSince && debouncedText.dateFilter === urlDate && debouncedText.agent === urlAgent && debouncedText.operation === urlOperation) return;
    const next = new URLSearchParams(searchParamsRef.current);
    if (debouncedText.since) next.set('since', debouncedText.since); else next.delete('since');
    if (debouncedText.dateFilter) next.set('date', debouncedText.dateFilter); else next.delete('date');
    if (debouncedText.agent) next.set('agent', debouncedText.agent); else next.delete('agent');
    if (debouncedText.operation) next.set('operation', debouncedText.operation); else next.delete('operation');
    next.delete('page');
    setSearchParams(next, { replace: true });
  // 防抖 effect 只依赖防抖值；监听 URL 文本会在 URL 刚写入而防抖状态尚未切换时反写旧草稿。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedText]);
  const writeImmediate = (changes: Record<string, string | undefined>) => {
    const next = new URLSearchParams(searchParamsRef.current);
    // 离散筛选/翻页也必须带上尚未到时的文本草稿，避免一次交互丢掉用户刚输入的条件。
    Object.entries(draftRef.current).forEach(([key, value]) => value ? next.set(key, value) : next.delete(key));
    Object.entries(changes).forEach(([key, value]) => value ? next.set(key, value) : next.delete(key));
    setSearchParams(next);
  };
  const common = { since: urlSince || undefined, date: urlDate || undefined, agent: urlAgent || undefined, source: urlSource, status: urlStatus, operation: urlOperation || undefined, page: urlPage, page_size: pageSize };
  const eventsQuery = useAuditEvents(common);
  const events = eventsQuery.data?.data;
  // 列表响应已经包含同一筛选条件的精确 summary；直接复用避免历史 JSONL 被
  // 摘要请求和列表请求各读一遍，同时保证顶部数字与当前列表完全同源。
  const summary = events?.summary;
  const clear = () => setSearchParams(new URLSearchParams());
  return <>
    <PageHeader title="审计日志" description="查看本机 MCP、hook 与 Web 受控动作的安全摘要和有限详情。" />
    <WarningBar meta={eventsQuery.data?.meta} />
    {summary && <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}><Card><Statistic title="调用总数" value={summary.count} /></Card></Col>
        <Col xs={12} sm={6}><Card><Statistic title="成功" value={summary.statuses.succeeded ?? 0} /></Card></Col>
        <Col xs={12} sm={6}><Card><Statistic title="失败 / 未完成" value={(summary.statuses.failed ?? 0) + (summary.statuses.incomplete ?? 0)} /></Card></Col>
        <Col xs={12} sm={6}><Card><Statistic title="平均耗时" value={summary.average_duration_ms ?? 0} suffix="ms" /></Card></Col>
        <Col xs={24} lg={12}><Card title="状态分布"><Space wrap>{Object.entries(summary.statuses).map(([key, count]) => <Tag key={key}>{STATUS_LABELS[key as AuditStatus] ?? key}：{count}</Tag>)}</Space></Card></Col>
        <Col xs={24} lg={12}><Card title="来源与降级"><Space wrap><Tag>mcp：{summary.sources.mcp ?? 0}</Tag><Tag>hook：{summary.sources.hook ?? 0}</Tag><Tag>web：{summary.sources.web ?? 0}</Tag><Tag color={summary.fallback_records ? 'orange' : undefined}>fallback：{summary.fallback_records ?? 0}</Tag><Tag color={summary.damaged_count ? 'red' : undefined}>损坏：{summary.damaged_count ?? 0}</Tag></Space><Typography.Paragraph type="secondary">最近活动：{date(summary.last_activity)}</Typography.Paragraph></Card></Col>
    </Row>}
    <Card className="section-gap search-controls">
      <Space wrap>
        <Input aria-label="审计时间范围" placeholder="since，例如 24h 或 7d" value={since} onChange={(event) => { const value = event.target.value; draftRef.current = { ...draftRef.current, since: value, date: '' }; setSince(value); setDateFilter(''); }} />
        <Input aria-label="审计日期" placeholder="日期，例如 2026-08-29" value={dateFilter} onChange={(event) => { const value = event.target.value; draftRef.current = { ...draftRef.current, since: '', date: value }; setDateFilter(value); setSince(''); }} />
        <Input aria-label="按 Agent 筛选审计" placeholder="Agent" value={agent} onChange={(event) => { const value = event.target.value; draftRef.current = { ...draftRef.current, agent: value }; setAgent(value); }} />
        <Input aria-label="按操作筛选审计" placeholder="操作" value={operation} onChange={(event) => { const value = event.target.value; draftRef.current = { ...draftRef.current, operation: value }; setOperation(value); }} />
        <Select aria-label="按来源筛选审计" allowClear placeholder="全部来源" style={{ width: 130 }} value={source} onChange={(value) => writeImmediate({ source: value, page: undefined })} options={[{ value: 'mcp', label: 'MCP' }, { value: 'hook', label: 'Hook' }, { value: 'web', label: 'Web' }]} />
        <Select aria-label="按状态筛选审计" allowClear placeholder="全部状态" style={{ width: 150 }} value={status} onChange={(value) => writeImmediate({ status: value, page: undefined })} options={Object.entries(STATUS_LABELS).map(([valueKey, label]) => ({ value: valueKey, label }))} />
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
        {events.pagination.total !== null && events.pagination.total > pageSize && <AntPagination className="section-gap" current={page} pageSize={pageSize} total={events.pagination.total} showSizeChanger={false} onChange={(nextPage) => writeImmediate({ page: String(nextPage) })} />}
      </Card>}
    </AsyncState>
  </>;
}

function AuditDetail({ invocationId }: { invocationId: string }) {
  const navigate = useNavigate();
  const query = useAuditEvent(invocationId);
  const item = query.data?.data;
  return <>
    <PageHeader title="审计调用详情" description="有限安全投影；不展示原始 action、result 或诊断附件。" extra={<Button onClick={() => navigate('/audit')}>← 返回审计日志</Button>} />
    <WarningBar meta={query.data?.meta} />
    <AsyncState loading={query.isLoading} error={query.error} onRetry={() => void query.refetch()} empty={!item} emptyDescription="未找到该审计调用。">
      {item && <AuditEventCard item={item} />}
    </AsyncState>
  </>;
}

function AuditEventCard({ item }: { item: AuditEvent }) {
  return <Card title={<Space>{operationLabel(item.operation)} {statusTag(item.status)}</Space>}><Descriptions column={1} size="small"><Descriptions.Item label="调用 ID">{value(item.invocation_id)}</Descriptions.Item><Descriptions.Item label="事件 ID">{value(item.event_id)}</Descriptions.Item><Descriptions.Item label="开始时间">{date(item.started_at)}</Descriptions.Item><Descriptions.Item label="结束时间">{date(item.finished_at)}</Descriptions.Item><Descriptions.Item label="来源 / Agent">{value(item.source)} / {value(item.agent)}</Descriptions.Item><Descriptions.Item label="会话标签">{item.session_label ?? '未提供会话标签'}</Descriptions.Item><Descriptions.Item label="会话 ID">{session(item.session_id)}</Descriptions.Item><Descriptions.Item label="项目逻辑 ID">{value(item.project_id)}</Descriptions.Item><Descriptions.Item label="动作说明">{value(item.action_text)}</Descriptions.Item><Descriptions.Item label="结果说明">{value(item.result_text)}</Descriptions.Item><Descriptions.Item label="结果代码">{value(item.outcome_code)}</Descriptions.Item><Descriptions.Item label="错误类型">{value(item.error_type)}</Descriptions.Item><Descriptions.Item label="捕获级别">{value(item.capture_level)}</Descriptions.Item><Descriptions.Item label="耗时">{item.duration_ms == null ? '—' : `${item.duration_ms} ms`}</Descriptions.Item><Descriptions.Item label="降级记录">{item.fallback ? <Tag color="orange">是</Tag> : '否'}</Descriptions.Item></Descriptions></Card>;
}
