import { Alert, Button, Card, Col, Descriptions, Empty, Input, List, Pagination as AntPagination, Row, Select, Space, Tag, Typography } from 'antd';
import { Link, useParams } from 'react-router-dom';
import { AsyncState } from '../components/AsyncState';
import { PageHeader } from '../components/PageHeader';
import { useRuntimeCheckpoint, useRuntimeCheckpoints, useRuntimeWorkingState, useRuntimeWorkingStates } from '../hooks/useApi';
import type { ApiMeta, CheckpointDetail, RuntimeRepositorySummary, WorkingStateStatus } from '../types/api';
import { useState } from 'react';

const STATUS_LABELS: Record<WorkingStateStatus, string> = { planned: '计划中', active: '进行中', blocked: '已阻塞' };
const STATUS_COLORS: Record<WorkingStateStatus, string> = { planned: 'blue', active: 'green', blocked: 'red' };

/** 将后端降级元信息转为稳定的人类提示；绝不展示底层路径、异常或诊断正文。 */
function WarningBar({ meta, audit = false }: { meta?: ApiMeta; audit?: boolean }) {
  const labels: Record<string, string> = {
    index_unavailable: '索引不可用，当前结果可能不完整。',
    audit_partial: '审计记录部分不可读，当前仅展示可信记录。',
    damaged_records: '存在损坏记录，损坏详情仅保留计数。',
    session_id_unavailable: '部分记录未提供真实会话 ID。',
  };
  const warnings = (meta?.warnings ?? []).map((warning) => labels[warning] ?? '部分数据处于降级状态。');
  if (!warnings.length && !meta?.degraded) return null;
  return <Alert className="section-gap" type="warning" showIcon message={audit ? '审计数据部分降级' : '运行状态部分降级'} description={warnings.length ? [...new Set(warnings)].join(' ') : '部分数据不可用，但当前安全子集仍可查看。'} />;
}

function StatusTag({ status }: { status?: string | null }) {
  if (!status) return <Tag>未知状态</Tag>;
  const typed = status as WorkingStateStatus;
  return <Tag color={STATUS_COLORS[typed] ?? 'default'}>{STATUS_LABELS[typed] ?? status}</Tag>;
}

function text(value: unknown): string {
  if (Array.isArray(value)) return value.join('、');
  return value === null || value === undefined || value === '' ? '—' : String(value);
}

function date(value?: string | null): string {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN');
}

function SessionId({ value }: { value?: string | null }) { return <>{value ?? '未提供会话 ID'}</>; }

/** 用固定标签区分可信 owner 与最新作者，避免兼容字段造成责任主体混淆。 */
function OwnershipTag({ mode }: { mode?: string | null }) {
  const labels: Record<string, [string, string | undefined]> = {
    'session-bound': ['会话绑定', 'green'],
    shared: ['共享授权', 'blue'],
    'handed-off': ['已交接', 'purple'],
    'legacy-unbound': ['旧数据·未认领', 'orange'],
  };
  const [label, color] = labels[mode ?? 'legacy-unbound'] ?? ['归属状态未知', 'orange'];
  return <Tag color={color}>{label}</Tag>;
}

function Repositories({ repositories }: { repositories?: RuntimeRepositorySummary[] }) {
  if (!repositories?.length) return <Typography.Text type="secondary">暂无仓库摘要</Typography.Text>;
  return <List size="small" dataSource={repositories} renderItem={(repo) => <List.Item><Space><Typography.Text strong>{repo.role === 'control' ? '控制仓' : repo.role === 'knowledge' ? '知识仓' : repo.role}</Typography.Text><Typography.Text type="secondary">{repo.branch ?? '未知分支'} · {repo.revision ?? '无 revision'}</Typography.Text>{repo.dirty ? <Tag color="orange">有改动</Tag> : <Tag color="green">干净</Tag>}</Space></List.Item>} />;
}

function Sections({ sections }: { sections?: Record<string, string | string[] | null> }) {
  const entries = Object.entries(sections ?? {}).filter(([, value]) => value !== null && value !== undefined && value !== '');
  if (!entries.length) return <Typography.Text type="secondary">暂无可展示的恢复章节</Typography.Text>;
  return <List size="small" dataSource={entries} renderItem={([name, value]) => <List.Item><div><Typography.Text strong>{name}</Typography.Text><Typography.Paragraph className="runtime-section-value">{text(value)}</Typography.Paragraph></div></List.Item>} />;
}

/** 运行状态页面同时承担列表、任务详情和检查点深链，刷新深层 URL 仍会回到同一只读页面。 */
export function RuntimePage() {
  const { workId, checkpointId } = useParams<{ workId?: string; checkpointId?: string }>();
  return workId ? <RuntimeDetail workId={workId} checkpointId={checkpointId} /> : <RuntimeList />;
}

function RuntimeList() {
  const [project, setProject] = useState('');
  const [agent, setAgent] = useState('');
  const [status, setStatus] = useState<string>();
  const [page, setPage] = useState(1);
  const pageSize = 20;
  const query = useRuntimeWorkingStates({ project_id: project || undefined, agent: agent || undefined, status, page, page_size: pageSize });
  const data = query.data?.data;
  return <>
    <PageHeader title="运行状态" description="查看活动 Working State、有限恢复章节和双仓安全摘要。" />
    <Card className="search-controls">
      <Space wrap>
        <Input aria-label="按项目筛选" placeholder="项目逻辑 ID" value={project} onChange={(event) => { setProject(event.target.value); setPage(1); }} />
        <Input aria-label="按最新作者筛选" placeholder="最新检查点作者 Agent" value={agent} onChange={(event) => { setAgent(event.target.value); setPage(1); }} />
        <Select aria-label="按任务状态筛选" allowClear placeholder="全部活动状态" style={{ width: 170 }} value={status} onChange={(value) => { setStatus(value); setPage(1); }} options={Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label }))} />
        <Button onClick={() => { setProject(''); setAgent(''); setStatus(undefined); setPage(1); }}>清除筛选</Button>
      </Space>
    </Card>
    <WarningBar meta={query.data?.meta} />
    <AsyncState loading={query.isLoading} error={query.error} onRetry={() => void query.refetch()} empty={!data}>
      {data && <Card title={<span>活动任务 <Typography.Text type="secondary">{data.pagination.total ?? data.items.length} 项</Typography.Text></span>}>
        {!data.items.length ? <Empty description="当前没有活动任务。" /> : <List itemLayout="vertical" dataSource={data.items} renderItem={(item) => <List.Item actions={[<Link key="detail" to={`/runtime/${encodeURIComponent(item.work_id)}`}>查看详情</Link>]}> <List.Item.Meta title={<Space><Link to={`/runtime/${encodeURIComponent(item.work_id)}`}>{item.work_id}</Link><StatusTag status={item.status} /><OwnershipTag mode={item.ownership_mode} /></Space>} description={<Space wrap><Typography.Text type="secondary">项目：{text(item.project_id)}</Typography.Text><Typography.Text type="secondary">Owner：{text(item.owner_agent)}</Typography.Text><Typography.Text type="secondary">最新作者：{text(item.author_agent ?? item.agent)}</Typography.Text><Typography.Text type="secondary">参与会话：{item.participant_count ?? item.participants?.length ?? 0}</Typography.Text></Space>} /><Typography.Paragraph ellipsis={{ rows: 2 }}>{text(item.goal)}</Typography.Paragraph><Space wrap><Typography.Text type="secondary">更新：{date(item.updated_at)}</Typography.Text><Typography.Text type="secondary">检查点：{text(item.checkpoint_id)}</Typography.Text>{item.workspace_dirty ? <Tag color="orange">工作区有改动</Tag> : <Tag color="green">工作区干净</Tag>}</Space></List.Item>} />}
        {data.pagination.total !== null && data.pagination.total > pageSize && <AntPagination className="section-gap" current={page} pageSize={pageSize} total={data.pagination.total} showSizeChanger={false} onChange={setPage} />}
      </Card>}
    </AsyncState>
  </>;
}

function RuntimeDetail({ workId, checkpointId }: { workId: string; checkpointId?: string }) {
  const detailQuery = useRuntimeWorkingState(workId);
  const [checkpointPage, setCheckpointPage] = useState(1);
  const checkpointPageSize = 20;
  // 检查点是独立分页资源；详情深链打开时仍保留用户当前所在页，避免固定请求第一页。
  const checkpointsQuery = useRuntimeCheckpoints(workId, { page: checkpointPage, page_size: checkpointPageSize });
  const checkpointQuery = useRuntimeCheckpoint(workId, checkpointId);
  const detail = detailQuery.data?.data;
  const checkpoint = checkpointQuery.data?.data;
  return <>
    <PageHeader title={detail?.work_id ?? '任务详情'} description="仅展示活动任务的安全恢复信息。" extra={<Button href="/runtime">← 返回活动任务</Button>} />
    <WarningBar meta={detailQuery.data?.meta} />
    <AsyncState loading={detailQuery.isLoading} error={detailQuery.error} onRetry={() => void detailQuery.refetch()} empty={!detail} emptyDescription="未找到该任务或它已不在活动范围内。">
      {detail && <>
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={15}><Card title={<Space><span>任务概览</span><StatusTag status={detail.status} /><OwnershipTag mode={detail.ownership_mode} /></Space>}><Descriptions column={1} size="small"><Descriptions.Item label="目标">{text(detail.goal)}</Descriptions.Item><Descriptions.Item label="当前状态">{text(detail.current_state)}</Descriptions.Item><Descriptions.Item label="下一步">{text(detail.next_steps)}</Descriptions.Item><Descriptions.Item label="阻塞">{text(detail.blockers)}</Descriptions.Item><Descriptions.Item label="更新时间">{date(detail.updated_at)}</Descriptions.Item><Descriptions.Item label="Owner">{text(detail.owner_agent)} / <SessionId value={detail.owner_session_id} /></Descriptions.Item><Descriptions.Item label="最新作者">{text(detail.author_agent ?? detail.agent)} / {text(detail.author_role ?? detail.role)}</Descriptions.Item><Descriptions.Item label="作者会话 ID"><SessionId value={detail.author_session_id ?? detail.session_id} /></Descriptions.Item><Descriptions.Item label="授权参与会话">{detail.participant_count ?? detail.participants?.length ?? 0}</Descriptions.Item></Descriptions></Card></Col>
          <Col xs={24} lg={9}><Card title="双仓摘要"><Repositories repositories={detail.repositories} /></Card><Card title="有限恢复摘要" className="section-gap"><Typography.Paragraph>{text(detail.resume_capsule)}</Typography.Paragraph><Typography.Text type="secondary">详情状态：{text(detail.detail_status)} · 敏感级别：{text(detail.sensitivity)}</Typography.Text></Card></Col>
          <Col xs={24}><Card title={<span>恢复章节 <Typography.Text type="secondary">（只读白名单）</Typography.Text></span>}><Sections sections={detail.sections} /></Card></Col>
        </Row>
        <Card className="section-gap" title={<span>检查点历史 <Typography.Text type="secondary">{detail.checkpoint_count ?? checkpointsQuery.data?.data.pagination.total ?? 0} 项</Typography.Text></span>}>
          {checkpointsQuery.isLoading ? <Typography.Text type="secondary">正在读取检查点…</Typography.Text> : checkpointsQuery.error ? <Alert type="warning" showIcon message="检查点暂时不可用" description={checkpointsQuery.error.message} /> : !checkpointsQuery.data?.data.items.length ? <Empty description="当前任务没有检查点。" /> : <><List dataSource={checkpointsQuery.data.data.items} renderItem={(item) => <List.Item actions={[<Link key="open" to={`/runtime/${encodeURIComponent(workId)}/checkpoints/${encodeURIComponent(item.checkpoint_id)}`}>查看检查点</Link>]}><List.Item.Meta title={<Space><Typography.Text code>{item.checkpoint_id}</Typography.Text><StatusTag status={item.status} /></Space>} description={<Space wrap><Typography.Text type="secondary">更新时间：{date(item.updated_at)}</Typography.Text><Typography.Text type="secondary">作者：{text(item.author_agent ?? item.agent)}</Typography.Text><Typography.Text type="secondary">作者会话：<SessionId value={item.author_session_id ?? item.session_id} /></Typography.Text></Space>} /></List.Item>} />
            {(() => { const pagination = checkpointsQuery.data?.data.pagination; if (!pagination) return null; const total = pagination.total ?? (pagination.has_next ? (checkpointPage + 1) * checkpointPageSize : checkpointPage * checkpointPageSize); return <AntPagination className="section-gap" current={checkpointPage} pageSize={checkpointPageSize} total={total} showSizeChanger={false} onChange={setCheckpointPage} />; })()}
          </>}
        </Card>
        {checkpointId && <CheckpointDetailPanel loading={checkpointQuery.isLoading} error={checkpointQuery.error} checkpoint={checkpoint} />}
      </>}
    </AsyncState>
  </>;
}

function CheckpointDetailPanel({ loading, error, checkpoint }: { loading: boolean; error: Error | null; checkpoint?: CheckpointDetail }) {
  if (loading) return <Card className="section-gap"><Typography.Text type="secondary">正在读取检查点详情…</Typography.Text></Card>;
  if (error) return <Card className="section-gap"><Alert type="error" showIcon message="未找到该检查点或它已不可用" description={error.message} /></Card>;
  if (!checkpoint) return null;
  return <Card className="section-gap" title={<Space><span>检查点详情</span><StatusTag status={checkpoint.status} /></Space>}><Descriptions column={1} size="small"><Descriptions.Item label="检查点 ID">{checkpoint.checkpoint_id}</Descriptions.Item><Descriptions.Item label="基于检查点">{text(checkpoint.based_on)}</Descriptions.Item><Descriptions.Item label="更新时间">{date(checkpoint.updated_at)}</Descriptions.Item><Descriptions.Item label="检查点作者">{text(checkpoint.author_agent ?? checkpoint.agent)} / {text(checkpoint.author_role ?? checkpoint.role)}</Descriptions.Item><Descriptions.Item label="作者会话 ID"><SessionId value={checkpoint.author_session_id ?? checkpoint.session_id} /></Descriptions.Item><Descriptions.Item label="目标">{text(checkpoint.goal)}</Descriptions.Item><Descriptions.Item label="当前状态">{text(checkpoint.current_state)}</Descriptions.Item><Descriptions.Item label="下一步">{text(checkpoint.next_steps)}</Descriptions.Item><Descriptions.Item label="阻塞">{text(checkpoint.blockers)}</Descriptions.Item><Descriptions.Item label="验证">{text(checkpoint.verification)}</Descriptions.Item><Descriptions.Item label="变更文件"><Sections sections={{ changed_files: checkpoint.changed_files ?? null }} /></Descriptions.Item></Descriptions><Typography.Title level={5}>白名单章节</Typography.Title><Sections sections={checkpoint.sections} />{checkpoint.truncated && <Alert className="section-gap" type="warning" showIcon message="部分章节已裁剪" />}</Card>;
}
