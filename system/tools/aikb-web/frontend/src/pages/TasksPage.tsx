import { Alert, Button, Card, Col, Descriptions, Empty, List, Progress, Row, Space, Tag, Typography } from 'antd';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useEffect, useRef, useState } from 'react';
import { AsyncState } from '../components/AsyncState';
import { PageHeader } from '../components/PageHeader';
import { useActions, useCancelTask, useCreateTask, usePreviewAction, useTask, useTaskEvents, useTasks } from '../hooks/useApi';
import type { ActionPreview, ActionPreviewData, ActionSpec, ApiMeta, TaskEvent, TaskSnapshot, TaskStatus } from '../types/api';

const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  queued: '排队中', running: '运行中', cancelling: '取消中', succeeded: '成功',
  failed: '失败', timed_out: '已超时', cancelled: '已取消', interrupted: '已中断',
};
const TASK_STATUS_COLORS: Record<TaskStatus, string> = {
  queued: 'blue', running: 'processing', cancelling: 'orange', succeeded: 'green',
  failed: 'red', timed_out: 'red', cancelled: 'default', interrupted: 'gold',
};
const TERMINAL_STATUSES = new Set<TaskStatus>(['succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted']);

/** 任务页面的降级文案只接受机器 warning 枚举，不展示服务端异常、命令或路径。 */
function WarningBar({ meta }: { meta?: ApiMeta }) {
  const labels: Record<string, string> = { task_partial: '任务数据部分不可读。', events_partial: '任务事件部分不可读，当前展示最近安全快照。' };
  const warnings = (meta?.warnings ?? []).map((warning) => labels[warning] ?? '任务数据处于降级状态。');
  if (!meta?.degraded && !warnings.length) return null;
  return <Alert className="section-gap" type="warning" showIcon message="任务数据部分降级" description={[...new Set(warnings)].join(' ') || '部分数据不可用，但当前安全子集仍可查看。'} />;
}

function statusTag(status?: string | null) {
  const typed = status as TaskStatus;
  return <Tag color={TASK_STATUS_COLORS[typed] ?? 'default'}>{TASK_STATUS_LABELS[typed] ?? '状态未说明'}</Tag>;
}

function displayDate(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN');
}

function riskLabel(value?: string | null): string {
  return value === 'read_only' ? '只读动作' : value === 'derived_write' ? '派生写入' : value === 'user_config_write' ? '用户配置写入' : '风险级别未说明';
}
function riskColor(value?: string | null): string {
  return value === 'read_only' ? 'green' : value === 'user_config_write' ? 'red' : 'orange';
}
function platformLabel(platform: string): string { return platform === 'windows' ? 'Windows' : platform === 'macos' ? 'macOS' : '受支持平台'; }
function effectLabel(effect: string): string { return effect === 'read:control_repository' ? '读取控制仓' : effect === 'read:knowledge_repository' ? '读取知识仓' : '受控安全范围'; }
function isTerminal(status?: string | null): boolean { return Boolean(status && TERMINAL_STATUSES.has(status as TaskStatus)); }

/** 对服务端安全投影再做一层浏览器显示裁剪，防止未来字段扩展时意外渲染路径或句柄。 */
function safeText(value: unknown): string {
  return String(value ?? '')
    .replace(/(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\\\)[^\n\r<>"']+/g, '[本地路径]')
    .replace(/(?<![A-Za-z0-9_])\/(?:Users|home|private|workspace|tmp|var|etc|opt|root|mnt)(?:\/[^\s<>"']*)*/gi, '[本地路径]')
    .replace(/(password|secret|token|authorization|cookie)\s*[:=]\s*[^\s,;]+/gi, '$1=[已隐藏]');
}

function safeResult(value: unknown): string {
  if (value === null || value === undefined || value === '') return '暂无结构化结果';
  if (typeof value === 'string') return safeText(value);
  try {
    return safeText(JSON.stringify(value, (key, item) => /^(command|cmd|script|path|cwd|working_directory|environment|env|pid|handle|token|authorization|cookie|traceback)$/i.test(key) ? undefined : item, 2));
  } catch { return '结果无法展示'; }
}

/** 预览只展示语义步骤；若服务端未来误带命令细节，前端以固定文案遮蔽它。 */
function semanticStep(step: unknown): string {
  const value = String(step ?? '');
  return /(?:pwsh|powershell|git|python)(?:\.exe)?\s|(?:-file|--[a-z]|[A-Za-z]:[\\/]|\\\\|\/(?:users|home|workspace)\/)/i.test(value)
    ? '服务端受控步骤（命令细节不展示）'
    : safeText(value);
}

/** 将多个安全语义步骤收敛为描述表内联文本，避免列表组件的块级留白破坏标签与内容的基线对齐。 */
function semanticStepsText(steps?: unknown[]): string {
  return steps?.length ? steps.map(semanticStep).join('；') : '暂无步骤说明';
}

/** 动作卡片只暴露注册表安全摘要和服务端能力，不生成参数输入框。 */
function ActionCard({ action, onPreview, loading }: { action: ActionSpec; onPreview: (action: ActionSpec) => void; loading: boolean }) {
  const supported = action.supported ?? action.supported_platforms.includes('windows');
  return <Card className="task-action-card" title={<Space><span>{action.title}</span><Tag>{action.action_id}</Tag></Space>}>
    <Typography.Paragraph>{action.description}</Typography.Paragraph>
    <Descriptions column={1} size="small">
      <Descriptions.Item label="风险 / 影响"><Tag color={riskColor(action.risk_level)}>{riskLabel(action.risk_level)}</Tag>{action.effects?.length ? ` · ${action.effects.map(effectLabel).join('、')}` : ' · 无额外影响说明'}</Descriptions.Item>
      <Descriptions.Item label="超时">{action.timeout_seconds} 秒</Descriptions.Item>
      <Descriptions.Item label="平台">{action.supported_platforms?.length ? action.supported_platforms.map(platformLabel).join('、') : '平台未说明'}</Descriptions.Item>
      <Descriptions.Item label="参数">无（首批动作固定空 Schema，不允许自定义参数）</Descriptions.Item>
    </Descriptions>
    {!supported && <Alert type="warning" showIcon message="当前平台不支持" description={action.reason ?? '该动作暂不可用。'} />}
    <Button className="section-gap" type="primary" disabled={!supported || loading} loading={loading} onClick={() => onPreview(action)}>查看并预览</Button>
  </Card>;
}

function PreviewPanel({ preview, tokenReady, expires, submitting, onSubmit, onClose }: { preview: ActionPreview; tokenReady: boolean; expires?: number; submitting: boolean; onSubmit: () => void; onClose: () => void }) {
  const [confirmed, setConfirmed] = useState(false);
  // read_only 仍必须携带服务端令牌，但不增加第二次危险确认；派生写入才显示人工确认框。
  const needsConfirmation = preview.risk_level !== 'read_only';
  return <Card className="section-gap task-preview-card" title={<Space><span>执行预览</span><Tag color={riskColor(preview.risk_level)}>{riskLabel(preview.risk_level)}</Tag></Space>} extra={<Button type="link" onClick={onClose}>关闭预览</Button>}>
    <Alert type="info" showIcon message={preview.risk_level === 'read_only' ? '这是只读动作' : '这是受控派生写入动作'} description="必须先查看服务端规范化预览；页面不会展示或接收命令、路径、环境变量和自定义参数。" />
    <Descriptions className="section-gap" column={1} size="small">
      <Descriptions.Item label="动作">{preview.action_id}</Descriptions.Item>
      <Descriptions.Item label="规范化参数">无（空 Schema）</Descriptions.Item>
      <Descriptions.Item label="语义步骤"><span className="task-preview-steps">{semanticStepsText(preview.steps)}</span></Descriptions.Item>
      <Descriptions.Item label="影响范围">{preview.effects?.map(effectLabel).join('、') || '无额外影响说明'}</Descriptions.Item>
      <Descriptions.Item label="超时">{preview.timeout_seconds} 秒</Descriptions.Item>
      <Descriptions.Item label="预览摘要">{preview.preview_digest}</Descriptions.Item>
    </Descriptions>
    {needsConfirmation && <label className="task-confirmation"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /> 我已阅读上述预览并确认执行</label>}
    <Space className="section-gap"><Button type="primary" disabled={!tokenReady || (needsConfirmation && !confirmed)} loading={submitting} onClick={onSubmit}>执行预览</Button><Typography.Text type="secondary">{tokenReady ? `确认令牌有效期约 ${expires ?? 300} 秒` : '确认令牌未就绪'}</Typography.Text></Space>
  </Card>;
}

/** 任务中心主路由；列表和详情拆成组件以保持深层 URL 刷新可恢复。 */
export function TasksPage() {
  const { taskId } = useParams<{ taskId?: string }>();
  return taskId ? <TaskDetail taskId={taskId} /> : <TaskCenter />;
}

function TaskCenter() {
  const navigate = useNavigate();
  const actionsQuery = useActions();
  const tasksQuery = useTasks();
  const previewMutation = usePreviewAction();
  const createMutation = useCreateTask();
  const [previewAction, setPreviewAction] = useState<ActionSpec>();
  const [acceptedPreviewData, setAcceptedPreviewData] = useState<ActionPreviewData>();
  const previewGeneration = useRef(0);
  const previewData = acceptedPreviewData;
  const actions = actionsQuery.data?.data.items ?? [];
  const tasks = tasksQuery.data?.data.items ?? [];
  const requestPreview = (action: ActionSpec) => {
    const generation = ++previewGeneration.current;
    setPreviewAction(action);
    setAcceptedPreviewData(undefined);
    previewMutation.reset();
    previewMutation.mutate(action.action_id, { onSuccess: (response) => { if (generation === previewGeneration.current && response.data.preview.action_id === action.action_id) setAcceptedPreviewData(response.data); } });
  };
  const submitPreview = () => {
    if (!previewData || !previewAction || previewData.preview.action_id !== previewAction.action_id) return;
    const generation = previewGeneration.current;
    const submittedActionId = previewAction.action_id;
    createMutation.mutate({ action_id: previewData.preview.action_id, parameters: previewData.preview.parameters, preview_digest: previewData.preview.preview_digest, confirmation_token: previewData.confirmation_token }, { onSuccess: (response) => { if (generation !== previewGeneration.current || submittedActionId !== previewAction?.action_id) return; const id = response.data.task.task_id; if (id) navigate(`/tasks/${encodeURIComponent(id)}`); } });
  };
  return <>
    <PageHeader title="任务中心" description="通过服务端预览运行已注册的本机只读动作，并观察任务安全状态。" />
    <WarningBar meta={actionsQuery.data?.meta ?? tasksQuery.data?.meta} />
    <Card title="可用动作" className="section-gap">
      <AsyncState loading={actionsQuery.isLoading} error={actionsQuery.error} onRetry={() => void actionsQuery.refetch()} empty={!actionsQuery.data}>
        {!actions.length ? <Empty description="当前没有可用动作。" /> : <Row gutter={[16, 16]}>{actions.map((action) => <Col key={action.action_id} xs={24} lg={12} xl={8}><ActionCard action={action} onPreview={requestPreview} loading={previewMutation.isPending && previewAction?.action_id === action.action_id} /></Col>)}</Row>}
      </AsyncState>
      {previewMutation.error && <Alert className="section-gap" type="error" showIcon message="预览失败" description={previewMutation.error.message} />}
      {previewData && previewAction && previewData.preview.action_id === previewAction.action_id && <PreviewPanel preview={previewData.preview} tokenReady={Boolean(previewData.confirmation_token)} expires={previewData.expires_in_seconds} submitting={createMutation.isPending} onSubmit={submitPreview} onClose={() => { previewGeneration.current += 1; setPreviewAction(undefined); setAcceptedPreviewData(undefined); previewMutation.reset(); }} />}
      {createMutation.error && <Alert className="section-gap" type="error" showIcon message="任务创建失败" description={createMutation.error.message} />}
    </Card>
    <Card title={<span>最近任务 <Typography.Text type="secondary">{tasksQuery.data?.data.total ?? tasks.length} 项</Typography.Text></span>} className="section-gap">
      <AsyncState loading={tasksQuery.isLoading} error={tasksQuery.error} onRetry={() => void tasksQuery.refetch()} empty={!tasksQuery.data}>
        {!tasks.length ? <Empty description="暂无任务记录。请先从上方动作卡片查看预览。" /> : <TaskList tasks={tasks} />}
      </AsyncState>
    </Card>
  </>;
}

function TaskList({ tasks }: { tasks: TaskSnapshot[] }) {
  return <List itemLayout="vertical" dataSource={tasks} renderItem={(task) => <List.Item actions={[<Link key="detail" to={`/tasks/${encodeURIComponent(task.task_id)}`}>查看详情</Link>]}><List.Item.Meta title={<Space><Link to={`/tasks/${encodeURIComponent(task.task_id)}`}>{task.action_id}</Link>{statusTag(task.status)}</Space>} description={<Space wrap><Typography.Text type="secondary">任务：{task.task_id}</Typography.Text><Typography.Text type="secondary">创建：{displayDate(task.created_at)}</Typography.Text><Typography.Text type="secondary">更新：{displayDate(task.updated_at)}</Typography.Text></Space>} /><Space wrap><Tag color={riskColor(task.risk_level)}>{riskLabel(task.risk_level)}</Tag>{task.output_truncated && <Tag color="orange">输出已裁剪</Tag>}<Typography.Text type="secondary">超时：{task.timeout_seconds ?? '—'} 秒</Typography.Text></Space></List.Item>} />;
}

function applyTaskEvent(current: TaskSnapshot, event: TaskEvent): TaskSnapshot {
  const incoming = event.task ?? event.snapshot;
  // replay_reset 的 snapshot 是服务端重新声明的完整事实，替换而非沿用上一任务字段。
  const next: TaskSnapshot = incoming ? event.replay_reset ? { ...incoming } : { ...current, ...incoming } : { ...current };
  if (event.status) next.status = event.status;
  if (event.progress !== undefined) next.progress = event.progress;
  if (event.type === 'output') {
    if (event.truncated === true) next.output_truncated = true;
    else next.output = `${next.output ?? ''}${safeText(event.text ?? event.output ?? '')}`;
  }
  if (event.type === 'result' && event.result !== undefined) next.result = event.result;
  next.last_event_id = event.event_id;
  return next;
}

function TaskDetail({ taskId }: { taskId: string }) {
  const taskQuery = useTask(taskId);
  const queriedTask = taskQuery.data?.data.task;
  // 查询切换期间可能短暂保留旧缓存；不让旧任务快照驱动新任务的 SSE。
  const baseTask = queriedTask?.task_id === taskId ? queriedTask : undefined;
  const [liveTask, setLiveTask] = useState<TaskSnapshot>();
  const appliedEvents = useRef(new WeakSet<TaskEvent>());
  useEffect(() => {
    appliedEvents.current = new WeakSet<TaskEvent>();
    setLiveTask(undefined);
  }, [taskId]);
  useEffect(() => { if (baseTask) setLiveTask(baseTask); }, [baseTask]);
  const activeTask = liveTask ?? baseTask;
  const events = useTaskEvents(taskId, Boolean(activeTask && !isTerminal(activeTask.status)));
  const cancelMutation = useCancelTask(taskId);
  useEffect(() => {
    // Hook 在溢出时会用新的 replay_reset 检查点替换队列，按对象去重可兼容该有界压缩。
    const pending = events.events.filter((event) => !appliedEvents.current.has(event));
    if (!pending.length) return;
    pending.forEach((event) => appliedEvents.current.add(event));
    // 一个响应块可能包含 snapshot、多个 output 和 result，必须按服务器顺序一次性折叠全部事件。
    setLiveTask((current) => current ? pending.reduce(applyTaskEvent, current) : current);
  }, [events.events, taskId]);
  const cancel = () => cancelMutation.mutate(undefined, { onSuccess: (response) => setLiveTask(response.data.task) });
  return <>
    <PageHeader title="任务详情" description="展示任务状态、有限输出和安全结果；不展示命令、路径、进程句柄或原始异常。" extra={<Button href="/tasks">← 返回任务中心</Button>} />
    <WarningBar meta={taskQuery.data?.meta} />
    <AsyncState loading={taskQuery.isLoading} error={taskQuery.error} onRetry={() => void taskQuery.refetch()} empty={!activeTask} emptyDescription="未找到该任务。">
      {activeTask && <>
        <Card title={<Space><span>{activeTask.action_id}</span>{statusTag(activeTask.status)}{events.connected && <Tag color="green">实时连接</Tag>}{events.error && <Tag color="orange">实时连接重试中</Tag>}</Space>} extra={!isTerminal(activeTask.status) ? <Button danger loading={cancelMutation.isPending} onClick={cancel}>{activeTask.status === 'cancelling' ? '再次请求取消' : '取消任务'}</Button> : undefined}>
          <Row gutter={[16, 16]}>
            <Col xs={24} lg={12}><Descriptions column={1} size="small"><Descriptions.Item label="任务 ID">{activeTask.task_id}</Descriptions.Item><Descriptions.Item label="动作">{activeTask.action_id}</Descriptions.Item><Descriptions.Item label="风险"><Tag color={riskColor(activeTask.risk_level)}>{riskLabel(activeTask.risk_level)}</Tag></Descriptions.Item><Descriptions.Item label="状态">{statusTag(activeTask.status)}</Descriptions.Item><Descriptions.Item label="创建时间">{displayDate(activeTask.created_at)}</Descriptions.Item><Descriptions.Item label="更新时间">{displayDate(activeTask.updated_at)}</Descriptions.Item><Descriptions.Item label="超时">{activeTask.timeout_seconds ?? '—'} 秒</Descriptions.Item><Descriptions.Item label="关联审计 ID">{activeTask.invocation_id ?? '未提供'}</Descriptions.Item></Descriptions></Col>
            <Col xs={24} lg={12}><Typography.Text strong>进度</Typography.Text><Progress className="task-progress" percent={typeof activeTask.progress === 'number' ? Math.max(0, Math.min(100, activeTask.progress)) : undefined} status={activeTask.status === 'failed' ? 'exception' : activeTask.status === 'succeeded' ? 'success' : undefined} /><Typography.Text type="secondary">参数：无（首批动作固定空 Schema）</Typography.Text></Col>
          </Row>
        </Card>
        {events.error && <Alert className="section-gap" type="warning" showIcon message="实时事件暂不可用" description="正在自动重连；页面仍保留最近一次安全快照。" />}
        <Row gutter={[16, 16]} className="section-gap"><Col xs={24} xl={15}><Card title="安全输出">{activeTask.output ? <pre className="task-output">{safeText(activeTask.output)}</pre> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无输出" />}{activeTask.output_truncated && <Alert className="section-gap" type="warning" showIcon message="输出已达到安全长度上限，部分内容未展示" />}</Card></Col><Col xs={24} xl={9}><Card title="结构化结果"><pre className="task-output">{safeResult(activeTask.result)}</pre></Card></Col></Row>
      </>}
    </AsyncState>
  </>;
}
