import { Alert, Button, Card, Checkbox, Col, Descriptions, Empty, List, Row, Space, Spin, Tag, Typography } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { useApplyMaintenance, useMaintenanceChange, useMaintenanceTarget, useMaintenanceTargets, useMaintenanceTargetStatuses, usePreviewMaintenance } from '../hooks/useApi';
import type { MaintenanceApplyData, MaintenanceChangeData, MaintenanceChangeStatus, MaintenanceDiffData, MaintenanceLeafData, MaintenancePreviewData, MaintenanceTargetDetail, MaintenanceTargetStatus, MaintenanceTargetSummary } from '../types/api';

const TARGET_ORDER = ['environment', 'agent.codex', 'agent.claude-code'] as const;
const TARGET_LABELS: Record<string, string> = { environment: '环境', 'agent.codex': 'Codex', 'agent.claude-code': 'Claude Code' };
const STATUS_VIEW: Record<string, { label: string; color: string; description: string }> = {
  ready: { label: '已就绪', color: 'green', description: '受管内容与当前版本一致。' },
  missing: { label: '尚未安装', color: 'orange', description: '受管目标缺失，可以在后续阶段预览安装。' },
  drifted: { label: '检测到漂移', color: 'gold', description: '受管内容与当前版本不一致，可以预览修复。' },
  conflict: { label: '存在冲突', color: 'red', description: '检测到非受管同名内容，当前不会覆盖。' },
  invalid: { label: '配置无效', color: 'red', description: '目标配置无法安全解析，当前不会猜测修复。' },
  unsupported: { label: '当前平台不支持', color: 'default', description: '当前平台没有阶段 4B 的可用实现。' },
  restart_required: { label: '需要重启', color: 'blue', description: '配置已写入但需要人工重启对应 Agent。' },
};
const DIFF_LABELS: Record<string, string> = { unchanged: '无变化', missing: '目标缺失', drifted: '受管内容将更新', conflict: '存在受管冲突', invalid: '受管内容无效' };
const TERMINAL_CHANGE_STATUSES = ['succeeded', 'rolled_back', 'recovery_required'];

/**
 * 预览只对确实需要安装或修复的目标开放；ready 已与期望版本一致，
 * restart_required 则表示写入已完成，两者再次请求预览都会制造无意义的 409 风险。
 */
function isPreviewableMaintenanceStatus(status?: MaintenanceTargetStatus): boolean {
  return status === 'missing' || status === 'drifted';
}

/** 将平台能力、基线和目标状态映射为明确的禁用原因，避免用户只能看到“不能点”。 */
function getPreviewAvailability({ status, baseFingerprint, inspectionSupported, previewSupported }: { status?: MaintenanceTargetStatus; baseFingerprint?: string; inspectionSupported: boolean; previewSupported: boolean }): { enabled: boolean; reason?: string } {
  if (!inspectionSupported) return { enabled: false, reason: '当前平台不支持只读检查。' };
  if (!previewSupported) return { enabled: false, reason: '当前平台不支持结构化预览。' };
  if (!baseFingerprint) return { enabled: false, reason: '当前状态缺少基线指纹，暂不可预览。' };
  switch (status) {
    case 'missing':
    case 'drifted':
      return { enabled: true };
    case 'ready':
      return { enabled: false, reason: '当前状态已就绪，无需预览。' };
    case 'restart_required':
      return { enabled: false, reason: '配置已写入，请手动重启对应 Agent，无需再次预览。' };
    case 'conflict':
      return { enabled: false, reason: '当前目标存在冲突，无法安全预览。' };
    case 'invalid':
      return { enabled: false, reason: '当前目标配置无效，无法安全预览。' };
    case 'unsupported':
      return { enabled: false, reason: '当前目标或平台不支持结构化预览。' };
    default:
      return { enabled: false, reason: '当前状态不具备安全预览条件。' };
  }
}

/** 阶段 4B 安装与修复页面：先看服务端预览，再逐目标确认并跟踪受控事务。 */
export function MaintenancePage() {
  const targetsQuery = useMaintenanceTargets();
  const previewMutation = usePreviewMaintenance();
  const applyMutation = useApplyMaintenance();
  const statusQueries = useMaintenanceTargetStatuses();
  const [selectedTargetId, setSelectedTargetId] = useState<typeof TARGET_ORDER[number]>('environment');
  const [preview, setPreview] = useState<MaintenancePreviewData>();
  const [previewCreatedAt, setPreviewCreatedAt] = useState<number>();
  const [confirmed, setConfirmed] = useState(false);
  const [applySubmitted, setApplySubmitted] = useState(false);
  const [applyResult, setApplyResult] = useState<MaintenanceApplyData>();
  const terminalRefreshKey = useRef<string>();
  const summaries = targetsQuery.data?.data.items ?? [];
  const platform = targetsQuery.data?.data.platform;
  const summaryMap = new Map(summaries.map((item) => [item.target_id, item]));
  const selectedSummary = summaryMap.get(selectedTargetId);
  const statusMap = new Map(statusQueries.map((query) => [query.data?.data.status.target_id ?? '', query.data?.data.status]));
  const detailQuery = useMaintenanceTarget(selectedTargetId);
  const detail = detailQuery.data?.data;
  const refetchDetail = detailQuery.refetch;
  const refetchTargets = targetsQuery.refetch;
  const previewChangeId = getPreviewChangeId(preview);
  const appliedChangeId = applyResult?.change_id ?? (applySubmitted ? previewChangeId : undefined);
  const changeQuery = useMaintenanceChange(appliedChangeId, Boolean(appliedChangeId));
  const changeResponse = changeQuery.data?.data;
  const change = changeResponse?.change;
  const previewExpired = usePreviewExpired(preview, previewCreatedAt);
  const resetPreview = previewMutation.reset;
  const resetApply = applyMutation.reset;

  useEffect(() => {
    setPreview(undefined); setPreviewCreatedAt(undefined); setConfirmed(false); setApplySubmitted(false); setApplyResult(undefined); terminalRefreshKey.current = undefined;
    resetPreview(); resetApply();
  }, [resetApply, resetPreview, selectedTargetId]);

  // 变更进入终态后刷新目标状态，避免页面继续使用旧基线生成预览。
  useEffect(() => {
    const status = change?.status;
    const refreshKey = appliedChangeId && status ? `${appliedChangeId}:${status}` : undefined;
    if (!refreshKey || !status || !TERMINAL_CHANGE_STATUSES.includes(status) || terminalRefreshKey.current === refreshKey) return;
    // 保留预览和任务结果同屏，便于用户核对本次事务；目标详情重新读取新基线。
    terminalRefreshKey.current = refreshKey; setConfirmed(false);
    void refetchDetail(); void refetchTargets();
  }, [appliedChangeId, change?.status, refetchDetail, refetchTargets]);

  /** 只提交详情中的基线指纹；页面没有正文、路径、命令或环境值输入框。 */
  const requestPreview = () => {
    const baseFingerprint = detail?.status.base_fingerprint ?? selectedSummary?.base_fingerprint;
    const currentStatus = detail?.status.status ?? selectedSummary?.status;
    if (!baseFingerprint || !isPreviewableMaintenanceStatus(currentStatus)) return;
    setPreview(undefined); setPreviewCreatedAt(undefined); setConfirmed(false); setApplySubmitted(false); setApplyResult(undefined); resetApply();
    previewMutation.mutate({ targetId: selectedTargetId, base_fingerprint: baseFingerprint }, { onSuccess: (response) => { setPreview(response.data); setPreviewCreatedAt(Date.now()); setConfirmed(false); } });
  };

  /** 仅在完整、未过期预览上提交变更 ID 和一次性令牌；不会自动重放。 */
  const submitApply = () => {
    const token = preview?.confirmation_token;
    if (!preview || !previewChangeId || !token || previewExpired || !confirmed || applySubmitted || !isApplySupported(preview.platform)) return;
    setApplySubmitted(true); applyMutation.mutate({ changeId: previewChangeId, confirmation_token: token }, { onSuccess: (response) => setApplyResult(response.data) });
  };

  /** 失败或过期后清理旧令牌，强制重新读取基线并生成新的预览。 */
  const restartAfterApplyFailure = () => {
    setPreview(undefined); setPreviewCreatedAt(undefined); setConfirmed(false); setApplySubmitted(false); setApplyResult(undefined); resetPreview(); resetApply(); void detailQuery.refetch();
  };

  return <>
    <PageHeader title="安装与修复" description="按目标查看 AIKB 用户环境和 Agent 配置状态；写入前必须完成逐目标预览和高风险确认。" extra={<Tag color="blue">阶段 4B · 受控维护</Tag>} />
    {platform && <PlatformCapabilityAlert platform={platform} />}
    <div className="maintenance-notice"><Alert type="info" showIcon message={isApplySupported(platform) ? '逐目标受控应用 · 不提供一键全部修复' : '当前仅开放状态查看和结构化预览'} description="每次只处理当前选中的固定目标；页面不接受路径、命令、配置正文或环境值输入。" /></div>
    <AsyncMaintenanceState loading={targetsQuery.isLoading} error={targetsQuery.error} empty={!summaries.length} onRetry={() => void targetsQuery.refetch()}>
      <Row gutter={[16, 16]} className="maintenance-layout"><Col xs={24} lg={8} xl={7}><Card title="固定维护目标" className="maintenance-target-list-card"><nav aria-label="维护目标目录" className="maintenance-target-list">{TARGET_ORDER.map((targetId) => { const item = summaryMap.get(targetId); const status = getStatusView(statusMap.get(targetId)?.status ?? item?.status); return <button type="button" key={targetId} className={`maintenance-target-item${targetId === selectedTargetId ? ' is-selected' : ''}`} onClick={() => setSelectedTargetId(targetId)}><span className="maintenance-target-item-title">{TARGET_LABELS[targetId]}</span><span className="maintenance-target-item-id">{targetId}</span><span className="maintenance-target-item-status"><Tag color={status.color}>{status.label}</Tag></span></button>; })}</nav></Card></Col><Col xs={24} lg={16} xl={17}><MaintenanceDetail detail={detail} fallback={selectedSummary} loading={detailQuery.isLoading} error={detailQuery.error} preview={preview} previewExpired={previewExpired} previewLoading={previewMutation.isPending} previewError={previewMutation.error} applySupported={isApplySupported(detail?.platform ?? platform)} applyLoading={applyMutation.isPending} applySubmitted={applySubmitted} applyError={applyMutation.error} applyResult={applyResult} change={change} changeResponse={changeResponse} changeLoading={changeQuery.isLoading} changeError={changeQuery.error} confirmed={confirmed} onConfirmChange={setConfirmed} onPreview={requestPreview} onApply={submitApply} onRetry={() => void detailQuery.refetch()} onRestart={restartAfterApplyFailure} /></Col></Row>
    </AsyncMaintenanceState>
  </>;
}

function AsyncMaintenanceState({ loading, error, empty, onRetry, children }: { loading: boolean; error: Error | null; empty: boolean; onRetry: () => void; children: React.ReactNode }) {
  if (loading) return <div className="state-panel"><Spin size="large" tip="正在读取维护状态…" /></div>;
  if (error) return <div className="state-panel"><Alert type="error" showIcon message="维护状态暂时不可用" description="无法读取固定维护目标，请稍后重试。" action={<Button onClick={onRetry}>重试</Button>} /></div>;
  if (empty) return <Empty className="state-panel" image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可用维护目标" />;
  return <>{children}</>;
}

interface MaintenanceDetailProps { detail?: MaintenanceTargetDetail; fallback?: MaintenanceTargetSummary; loading: boolean; error: Error | null; preview?: MaintenancePreviewData; previewExpired: boolean; previewLoading: boolean; previewError: Error | null; applySupported: boolean; applyLoading: boolean; applySubmitted: boolean; applyError: Error | null; applyResult?: MaintenanceApplyData; change?: MaintenanceChangeData; changeResponse?: { blocked?: boolean; recovery_required?: boolean; warning?: string | null }; changeLoading: boolean; changeError: Error | null; confirmed: boolean; onConfirmChange: (checked: boolean) => void; onPreview: () => void; onApply: () => void; onRetry: () => void; onRestart: () => void }

/** 展示目标状态、逻辑叶子、结构化差异和事务安全摘要，不渲染物理路径或配置正文。 */
function MaintenanceDetail(props: MaintenanceDetailProps) {
  const { detail, fallback, loading, error, preview, previewExpired, previewLoading, previewError, applySupported, applyLoading, applySubmitted, applyError, applyResult, change, changeResponse, changeLoading, changeError, confirmed, onConfirmChange, onPreview, onApply, onRetry, onRestart } = props;
  if (loading) return <Card title="维护目标详情"><div className="maintenance-detail-loading"><Spin tip="正在读取目标详情…" /></div></Card>;
  if (error || !detail && !fallback) return <Card title="维护目标详情"><Alert type="error" showIcon message="目标详情暂时不可用" description="当前目标的安全状态无法读取，请稍后重试。" action={<Button onClick={onRetry}>重试</Button>} /></Card>;
  const target = detail?.target ?? fallback; if (!target) return null;
  const state = detail?.status; const currentStatus = state?.status ?? target.status; const status = getStatusView(currentStatus); const baseFingerprint = state?.base_fingerprint ?? target.base_fingerprint;
  const inspectionSupported = detail?.platform.inspection_supported ?? detail?.platform.supported ?? true; const previewSupported = detail?.platform.preview_supported ?? inspectionSupported;
  const previewAvailability = getPreviewAvailability({ status: currentStatus, baseFingerprint: baseFingerprint ?? undefined, inspectionSupported, previewSupported }); const canPreview = previewAvailability.enabled; const leaves: MaintenanceLeafData[] = detail?.leaves ?? (state?.logical_leaves ?? target.logical_leaves ?? []).map((leaf_id) => ({ leaf_id }));
  const previewChangeId = getPreviewChangeId(preview); const canApply = Boolean(preview && previewChangeId && preview.confirmation_token && !previewExpired && applySupported && !applySubmitted);
  return <Card title={target.title ?? TARGET_LABELS[target.target_id] ?? '维护目标'} className="maintenance-detail-card"><Space wrap className="maintenance-detail-heading"><Tag color={status.color}>{status.label}</Tag><Typography.Text type="secondary">{target.target_id}</Typography.Text>{(target.restart_required || state?.restart_required || change?.restart_required || applyResult?.restart_required) && <Tag color="blue">需要人工重启</Tag>}</Space><Typography.Paragraph type="secondary">{status.description}</Typography.Paragraph>{(change?.restart_required || applyResult?.restart_required) ? <Alert className="section-gap" type="info" showIcon message="配置已更新，需要人工重启对应 Agent" description="页面不会尝试控制或关闭用户进程；请在任务完成后手动重启对应 Agent。" /> : null}<Descriptions column={1} size="small"><Descriptions.Item label="风险级别">{target.risk_level ?? 'user_config_write'}</Descriptions.Item><Descriptions.Item label="安全说明">{target.description}</Descriptions.Item>{baseFingerprint && <Descriptions.Item label="当前基线指纹"><Typography.Text copyable>{baseFingerprint}</Typography.Text></Descriptions.Item>}</Descriptions><Typography.Title level={5} className="section-gap">受管逻辑叶子</Typography.Title><List size="small" className="maintenance-leaf-list" dataSource={leaves} locale={{ emptyText: '暂无叶子状态' }} renderItem={(leaf) => <List.Item><span className="maintenance-leaf-id">{leaf.leaf_id}</span><Space>{leaf.existence && <Tag>{leaf.existence === 'missing' ? '缺失' : '存在'}</Tag>}{leaf.progress && <Tag color="blue">{leaf.progress}</Tag>}</Space></List.Item>} /><div className="maintenance-preview-action"><Button type="primary" ghost disabled={!canPreview || previewLoading} loading={previewLoading} onClick={onPreview}>查看结构化预览</Button>{!canPreview && <Typography.Text type="secondary">{previewAvailability.reason}</Typography.Text>}</div>{previewError && <Alert className="section-gap" type="error" showIcon message={getPreviewErrorTitle(previewError)} description={getPreviewErrorDescription(previewError)} />}{preview && <MaintenancePreview preview={preview} expired={previewExpired} applySupported={applySupported} confirmed={confirmed} applying={applyLoading} applySubmitted={applySubmitted} canApply={canApply} applyError={applyError} applyResult={applyResult} change={change} changeResponse={changeResponse} changeLoading={changeLoading} changeError={changeError} onConfirmChange={onConfirmChange} onApply={onApply} onRestart={onRestart} />}</Card>;
}

interface MaintenancePreviewProps { preview: MaintenancePreviewData; expired: boolean; applySupported: boolean; confirmed: boolean; applying: boolean; applySubmitted: boolean; canApply: boolean; applyError: Error | null; applyResult?: MaintenanceApplyData; change?: MaintenanceChangeData; changeResponse?: { blocked?: boolean; recovery_required?: boolean; warning?: string | null }; changeLoading: boolean; changeError: Error | null; onConfirmChange: (checked: boolean) => void; onApply: () => void; onRestart: () => void }

/** 只展示服务端安全摘要；确认区在预览完整且平台公开支持 apply 时才出现。 */
function MaintenancePreview({ preview, expired, applySupported, confirmed, applying, applySubmitted, canApply, applyError, applyResult, change, changeResponse, changeLoading, changeError, onConfirmChange, onApply, onRestart }: MaintenancePreviewProps) {
  const plan = preview.plan; const status = change?.status ?? applyResult?.status;
  return <div className="maintenance-preview-panel">{expired ? <Alert type="warning" showIcon message="预览已过期" description="请重新读取目标并生成预览；过期令牌不会执行任何写入。" /> : <Alert type="info" showIcon message="结构化预览已生成" description={applySupported ? '请完整审阅受管差异；确认后只会提交服务端生成的变更摘要。' : '当前部署未开放 apply，页面只展示安全元数据。'} />}<Descriptions column={1} size="small" className="section-gap">{getPreviewChangeId(preview) && <Descriptions.Item label="变更 ID">{getPreviewChangeId(preview)}</Descriptions.Item>}{plan?.preview_digest && <Descriptions.Item label="预览摘要"><Typography.Text copyable>{plan.preview_digest}</Typography.Text></Descriptions.Item>}{plan?.before_fingerprint && <Descriptions.Item label="变更前指纹"><Typography.Text copyable>{plan.before_fingerprint}</Typography.Text></Descriptions.Item>}{plan?.after_fingerprint && <Descriptions.Item label="变更后指纹"><Typography.Text copyable>{plan.after_fingerprint}</Typography.Text></Descriptions.Item>}{getPreviewExpiry(preview) && <Descriptions.Item label="有效至">{getPreviewExpiry(preview)}</Descriptions.Item>}</Descriptions>{plan?.steps?.length ? <><Typography.Title level={5} className="section-gap">固定步骤</Typography.Title><Space wrap>{plan.steps.map((step) => <Tag key={step.step_id}>{step.step_id}</Tag>)}</Space></> : null}<Typography.Title level={5} className="section-gap">受管差异</Typography.Title><List size="small" dataSource={plan?.differences ?? []} locale={{ emptyText: '受管内容无变化' }} renderItem={(diff) => <MaintenanceDiff diff={diff} />} />{canApply && <div className="maintenance-confirmation-zone"><Alert type="warning" showIcon message="高风险确认：即将写入用户配置" description="应用只处理当前目标的服务端固定步骤，可能更新用户环境或 Agent 配置；不会产生 Git 提交，也不会处理其他目标。" /><Checkbox checked={confirmed} onChange={(event) => onConfirmChange(event.target.checked)} className="maintenance-confirmation-checkbox">我已完整审阅受管差异，并确认仅对当前目标执行一次受控写入。</Checkbox><Button danger type="primary" disabled={!confirmed || applySubmitted} loading={applying} onClick={onApply}>{applySubmitted ? '已提交，等待结果' : '确认并应用当前目标'}</Button></div>}{preview && !applySupported && <Typography.Text type="secondary" className="maintenance-token-note">当前服务端未开放维护写入；预览不会创建或提交变更。</Typography.Text>}{applyError && <Alert className="section-gap" type="error" showIcon message={getApplyErrorTitle(applyError)} description={getApplyErrorDescription(applyError)} action={<Button onClick={onRestart}>重新读取并生成新预览</Button>} />}{applyResult && <MaintenanceApplyResult result={applyResult} status={status} change={change} changeResponse={changeResponse} loading={changeLoading} error={changeError} />}</div>;
}

/** 展示任务跳转、事务状态、回滚和恢复阻断；所有文案只依赖安全枚举。 */
function MaintenanceApplyResult({ result, status, change, changeResponse, loading, error }: { result: MaintenanceApplyData; status?: MaintenanceChangeStatus; change?: MaintenanceChangeData; changeResponse?: { blocked?: boolean; recovery_required?: boolean; warning?: string | null }; loading: boolean; error: Error | null }) {
  const taskId = change?.task_id ?? result.task_id ?? result.task?.task_id; if (loading && !status && !error) return <div className="maintenance-result-loading">正在读取维护任务状态…</div>; const view = maintenanceChangeStatusView(status); const blocked = Boolean(changeResponse?.blocked || changeResponse?.recovery_required || result.blocked || status === 'recovery_required');
  return <div className="maintenance-apply-result section-gap">{error && <Alert type="warning" showIcon message="正在读取维护变更状态" description="任务已提交，但当前无法读取最新状态；页面不会自动重复提交。" />}{blocked && <Alert type="error" showIcon message="维护写入已被恢复门禁阻断" description="系统要求人工检查恢复状态；页面不会自动重试或覆盖现场。" />}{changeResponse?.warning && <Alert type="warning" showIcon message="维护状态提示" description={changeResponse.warning} />}<Alert type={view.type} showIcon message={view.title} description={view.description} /><Descriptions column={1} size="small" className="section-gap"><Descriptions.Item label="变更 ID">{result.change_id}</Descriptions.Item>{taskId && <Descriptions.Item label="任务 ID">{taskId}</Descriptions.Item>}{change?.rollback_status && <Descriptions.Item label="回滚状态">{change.rollback_status}</Descriptions.Item>}</Descriptions>{taskId && <Button type="link"><Link to={`/tasks/${encodeURIComponent(taskId)}`}>查看任务中心</Link></Button>}</div>;
}

/**
 * 展示服务端固定语义说明，帮助用户在确认前理解“哪里变了、会做什么、
 * 哪些内容保留”。哈希仅作为折叠的次要证据，正文、路径和真实值永不渲染。
 */
function MaintenanceDiff({ diff }: { diff: MaintenanceDiffData }) {
  const affected = diff.affected_fields ?? [];
  const preserved = diff.preserved_scope ?? [];
  const managed = diff.managed_diff ?? [];
  return <List.Item>
    <div className="maintenance-diff-item">
      <div>
        <Typography.Text strong>{diff.display_name ?? diff.leaf_id}</Typography.Text>
        <Typography.Text type="secondary" className="maintenance-diff-status">{DIFF_LABELS[diff.difference_code ?? ''] ?? '受管状态已返回'}</Typography.Text>
      </div>
      <Descriptions column={1} size="small" className="maintenance-diff-semantics">
        {diff.current_summary && <Descriptions.Item label="当前问题">{diff.current_summary}</Descriptions.Item>}
        {diff.change_action && <Descriptions.Item label="将执行">{diff.change_action}</Descriptions.Item>}
        {diff.expected_summary && <Descriptions.Item label="预期结果">{diff.expected_summary}</Descriptions.Item>}
        {affected.length > 0 && <Descriptions.Item label="影响字段">{affected.join('、')}</Descriptions.Item>}
        {managed.length > 0 && <Descriptions.Item label="受管范围">{managed.join('、')}</Descriptions.Item>}
        {preserved.length > 0 && <Descriptions.Item label="明确保留">{preserved.join('、')}</Descriptions.Item>}
      </Descriptions>
      {(diff.before_hash || diff.after_hash) && <details className="maintenance-diff-evidence">
        <summary>显示摘要证据</summary>
        <Space direction="vertical" size={0}>
          {diff.before_hash && <Typography.Text type="secondary" className="maintenance-diff-hash">变更前摘要：{diff.before_hash}</Typography.Text>}
          {diff.after_hash && <Typography.Text type="secondary" className="maintenance-diff-hash">变更后摘要：{diff.after_hash}</Typography.Text>}
        </Space>
      </details>}
    </div>
  </List.Item>;
}

function PlatformCapabilityAlert({ platform }: { platform: import('../types/api').MaintenancePlatformData }) {
  const inspectionSupported = platform.inspection_supported ?? platform.supported; const previewSupported = platform.preview_supported ?? inspectionSupported; const applySupported = isApplySupported(platform);
  if (!inspectionSupported) return <Alert className="maintenance-platform-alert" type="warning" showIcon message="当前平台不支持安装与修复检查" description="当前平台没有已验证的只读检查或预览适配器，页面不会尝试读取配置位置。" />;
  return <Alert className="maintenance-platform-alert" type="info" showIcon message={`当前平台：${platform.platform} · 只读检查${previewSupported ? '与预览可用' : '可用'}`} description={applySupported ? '当前目标支持逐目标预览和高风险确认。' : '当前仅开放状态查看和结构化预览，安装、修复与写入仍未开放。'} />;
}
function isApplySupported(platform?: import('../types/api').MaintenancePlatformData): boolean { return Boolean(platform?.apply_supported ?? (platform?.supported && platform?.platform === 'windows')); }
function getStatusView(status?: MaintenanceTargetStatus) { return STATUS_VIEW[status ?? ''] ?? { label: '状态未知', color: 'default', description: '服务端未返回可识别状态；页面不会猜测或执行修复。' }; }
function getPreviewChangeId(preview?: MaintenancePreviewData): string | undefined { return preview?.change_id ?? preview?.change?.change_id; }
function getPreviewExpiry(preview?: MaintenancePreviewData): string | undefined { return preview?.expires_at ?? preview?.change?.expires_at; }

/** 令牌只在页面内存中使用；超时后确认区自动消失，避免误用陈旧预览。 */
function usePreviewExpired(preview?: MaintenancePreviewData, createdAt?: number): boolean {
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => { if (!preview) return undefined; const timer = window.setInterval(() => setClock(Date.now()), 1_000); return () => window.clearInterval(timer); }, [preview]);
  return useMemo(() => { if (!preview) return false; const expiresAt = getPreviewExpiry(preview); if (expiresAt) return Date.parse(expiresAt) <= clock; if (preview.expires_in_seconds !== undefined && createdAt !== undefined) return createdAt + preview.expires_in_seconds * 1_000 <= clock; return false; }, [clock, createdAt, preview]);
}
function maintenanceChangeStatusView(status?: MaintenanceChangeStatus): { type: 'success' | 'info' | 'warning' | 'error'; title: string; description: string } {
  switch (status) { case 'succeeded': return { type: 'success', title: '维护已成功应用', description: '当前目标的受控写入和复核已完成。若页面提示需要重启，请手动重启对应 Agent。' }; case 'rolled_back': return { type: 'warning', title: '应用失败，已成功回滚', description: '目标已恢复到应用前状态，请检查任务中心和审计安全摘要。' }; case 'recovery_required': return { type: 'error', title: '需要人工恢复', description: '系统检测到无法自动完成恢复，请根据系统状态和审计安全摘要人工处理。' }; case 'applying': return { type: 'info', title: '正在应用维护变更', description: '受控任务正在执行固定维护步骤，页面不会重复提交。' }; case 'verifying': return { type: 'info', title: '正在复核维护变更', description: '正在复核目标状态和安全摘要。' }; case 'rolling_back': return { type: 'warning', title: '正在回滚维护变更', description: '应用复核未完成，系统正在恢复应用前状态。' }; default: return { type: 'info', title: '维护任务已提交', description: '任务和变更事务已关联，正在等待服务端状态。' }; }
}
function getPreviewErrorTitle(error: Error): string { const code = 'code' in error ? String((error as Error & { code?: unknown }).code ?? '') : ''; if (code === 'MAINTENANCE_CONFLICT') return '目标存在冲突'; if (code === 'MAINTENANCE_TARGET_UNSUPPORTED') return '当前平台不支持'; return '结构化预览暂时不可用'; }
function getPreviewErrorDescription(error: Error): string { const code = 'code' in error ? String((error as Error & { code?: unknown }).code ?? '') : ''; if (code === 'MAINTENANCE_CONFLICT') return '检测到非受管内容或基线已变化；页面不会覆盖现场，请重新读取状态。'; if (code === 'MAINTENANCE_TARGET_UNSUPPORTED') return '当前平台没有已验证的维护适配器。'; return '服务端未能生成只读结构化预览，请稍后重试。'; }
function getApplyErrorTitle(error: Error): string { const code = 'code' in error ? String((error as Error & { code?: unknown }).code ?? '') : ''; if (code === 'MAINTENANCE_RECOVERY_REQUIRED') return '系统要求人工恢复'; if (code === 'MAINTENANCE_STALE_CHANGE') return '维护预览已失效'; return '维护应用暂时不可用'; }
function getApplyErrorDescription(error: Error): string { const code = 'code' in error ? String((error as Error & { code?: unknown }).code ?? '') : ''; if (code === 'MAINTENANCE_RECOVERY_REQUIRED') return '已有未完成或需要恢复的维护事务，页面不会自动重试或覆盖现场。'; if (code === 'MAINTENANCE_STALE_CHANGE') return '当前目标已发生变化，请重新读取状态并生成预览。'; return '服务端拒绝了该维护事务，请重新读取状态和预览。'; }
