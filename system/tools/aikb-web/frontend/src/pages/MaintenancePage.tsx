import { Alert, Button, Card, Col, Descriptions, Empty, List, Row, Space, Spin, Tag, Typography } from 'antd';
import { useEffect, useState } from 'react';
import { PageHeader } from '../components/PageHeader';
import { useMaintenanceTarget, useMaintenanceTargets, useMaintenanceTargetStatuses, usePreviewMaintenance } from '../hooks/useApi';
import type { MaintenanceDiffData, MaintenanceLeafData, MaintenanceTargetDetail, MaintenanceTargetStatus, MaintenanceTargetSummary } from '../types/api';

const TARGET_ORDER = ['environment', 'agent.codex', 'agent.claude-code'] as const;

const TARGET_LABELS: Record<string, string> = {
  environment: '环境',
  'agent.codex': 'Codex',
  'agent.claude-code': 'Claude Code',
};

const STATUS_VIEW: Record<string, { label: string; color: string; description: string }> = {
  ready: { label: '已就绪', color: 'green', description: '受管内容与当前版本一致。' },
  missing: { label: '尚未安装', color: 'orange', description: '受管目标缺失，可以在后续阶段预览安装。' },
  drifted: { label: '检测到漂移', color: 'gold', description: '受管内容与当前版本不一致，可以在后续阶段预览修复。' },
  conflict: { label: '存在冲突', color: 'red', description: '检测到非受管同名内容，当前不会覆盖。' },
  invalid: { label: '配置无效', color: 'red', description: '目标配置无法安全解析，当前不会猜测修复。' },
  unsupported: { label: '当前平台不支持', color: 'default', description: '当前平台没有阶段 4B 的可用实现。' },
  restart_required: { label: '需要重启', color: 'blue', description: '配置已写入但需要人工重启对应 Agent；当前页面仅提供只读预览。' },
};

const DIFF_LABELS: Record<string, string> = {
  unchanged: '无变化',
  missing: '目标缺失',
  drifted: '受管内容将更新',
  conflict: '存在受管冲突',
  invalid: '受管内容无效',
};

/** 阶段 4B 安装与修复只读页面：展示固定目标、逻辑叶子和安全结构化预览。 */
export function MaintenancePage() {
  const targetsQuery = useMaintenanceTargets();
  const previewMutation = usePreviewMaintenance();
  const statusQueries = useMaintenanceTargetStatuses();
  const [selectedTargetId, setSelectedTargetId] = useState<typeof TARGET_ORDER[number]>('environment');
  const [preview, setPreview] = useState<import('../types/api').MaintenancePreviewData>();
  const summaries = targetsQuery.data?.data.items ?? [];
  const platform = targetsQuery.data?.data.platform;
  const summaryMap = new Map(summaries.map((item) => [item.target_id, item]));
  const selectedSummary = summaryMap.get(selectedTargetId);
  const statusMap = new Map(statusQueries.map((query) => [query.data?.data.status.target_id ?? '', query.data?.data.status]));
  const detailQuery = useMaintenanceTarget(selectedTargetId);
  const detail = detailQuery.data?.data;

  const resetPreview = previewMutation.reset;
  useEffect(() => {
    setPreview(undefined);
    resetPreview();
  }, [resetPreview, selectedTargetId]);

  /** 只提交详情中的基线指纹；页面没有正文、路径、命令或环境值输入框。 */
  const requestPreview = () => {
    const baseFingerprint = detail?.status.base_fingerprint ?? selectedSummary?.base_fingerprint;
    if (!baseFingerprint || !selectedTargetId || detail?.status.status === 'unsupported') return;
    setPreview(undefined);
    previewMutation.mutate(
      { targetId: selectedTargetId, base_fingerprint: baseFingerprint },
      { onSuccess: (response) => setPreview(response.data) },
    );
  };

  return (
    <>
      <PageHeader
        title="安装与修复"
        description="按目标查看 AIKB 用户环境和 Agent 配置的只读状态；当前阶段不会写入或删除任何用户配置。"
        extra={<Tag color="blue">阶段 4B · 只读预览</Tag>}
      />
      {platform && <PlatformCapabilityAlert platform={platform} />}
      <div className="maintenance-notice"><Alert type="info" showIcon message="当前仅开放状态查看和结构化预览" description="应用、修复、卸载、路径输入、命令输入、配置正文和环境值输入均未开放。" /></div>
      <AsyncMaintenanceState loading={targetsQuery.isLoading} error={targetsQuery.error} empty={!summaries.length} onRetry={() => void targetsQuery.refetch()}>
        <Row gutter={[16, 16]} className="maintenance-layout">
          <Col xs={24} lg={8} xl={7}>
            <Card title="固定维护目标" className="maintenance-target-list-card">
              <nav aria-label="维护目标目录" className="maintenance-target-list">
                {TARGET_ORDER.map((targetId) => {
                  const item = summaryMap.get(targetId);
                  const status = getStatusView(statusMap.get(targetId)?.status ?? item?.status);
                  return (
                    <button
                      type="button"
                      key={targetId}
                      className={`maintenance-target-item${targetId === selectedTargetId ? ' is-selected' : ''}`}
                      onClick={() => setSelectedTargetId(targetId)}
                    >
                      <span className="maintenance-target-item-title">{TARGET_LABELS[targetId]}</span>
                      <span className="maintenance-target-item-id">{targetId}</span>
                      <span className="maintenance-target-item-status"><Tag color={status.color}>{status.label}</Tag></span>
                    </button>
                  );
                })}
              </nav>
            </Card>
          </Col>
          <Col xs={24} lg={16} xl={17}>
            <MaintenanceDetail
              detail={detail}
              fallback={selectedSummary}
              loading={detailQuery.isLoading}
              error={detailQuery.error}
              preview={preview}
              previewLoading={previewMutation.isPending}
              previewError={previewMutation.error}
              onPreview={requestPreview}
              onRetry={() => void detailQuery.refetch()}
            />
          </Col>
        </Row>
      </AsyncMaintenanceState>
    </>
  );
}

function AsyncMaintenanceState({ loading, error, empty, onRetry, children }: { loading: boolean; error: Error | null; empty: boolean; onRetry: () => void; children: React.ReactNode }) {
  if (loading) return <div className="state-panel"><Spin size="large" tip="正在读取维护状态…" /></div>;
  if (error) return <div className="state-panel"><Alert type="error" showIcon message="维护状态暂时不可用" description="无法读取固定维护目标，请稍后重试。" action={<Button onClick={onRetry}>重试</Button>} /></div>;
  if (empty) return <Empty className="state-panel" image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可用维护目标" />;
  return <>{children}</>;
}

interface MaintenanceDetailProps {
  detail?: MaintenanceTargetDetail;
  fallback?: MaintenanceTargetSummary;
  loading: boolean;
  error: Error | null;
  preview?: import('../types/api').MaintenancePreviewData;
  previewLoading: boolean;
  previewError: Error | null;
  onPreview: () => void;
  onRetry: () => void;
}

/** 展示一个目标的状态、逻辑叶子和受管差异；不渲染服务端可能携带的任意正文字段。 */
function MaintenanceDetail({ detail, fallback, loading, error, preview, previewLoading, previewError, onPreview, onRetry }: MaintenanceDetailProps) {
  if (loading) return <Card title="维护目标详情"><div className="maintenance-detail-loading"><Spin tip="正在读取目标详情…" /></div></Card>;
  if (error || !detail && !fallback) return <Card title="维护目标详情"><Alert type="error" showIcon message="目标详情暂时不可用" description="当前目标的安全状态无法读取，请稍后重试。" action={<Button onClick={onRetry}>重试</Button>} /></Card>;
  const target = detail?.target ?? fallback;
  if (!target) return null;
  const state = detail?.status;
  const currentStatus = state?.status ?? target.status;
  const status = getStatusView(currentStatus);
  const baseFingerprint = state?.base_fingerprint ?? target.base_fingerprint;
  const inspectionSupported = detail?.platform.inspection_supported ?? detail?.platform.supported ?? true;
  const previewSupported = detail?.platform.preview_supported ?? inspectionSupported;
  const canPreview = Boolean(baseFingerprint) && inspectionSupported && previewSupported && !['unsupported', 'conflict', 'invalid'].includes(currentStatus ?? '');
  const leaves: MaintenanceLeafData[] = detail?.leaves ?? (state?.logical_leaves ?? target.logical_leaves ?? []).map((leaf_id) => ({ leaf_id }));
  return (
    <Card title={target.title ?? TARGET_LABELS[target.target_id] ?? '维护目标'} className="maintenance-detail-card">
      <Space wrap className="maintenance-detail-heading">
        <Tag color={status.color}>{status.label}</Tag>
        <Typography.Text type="secondary">{target.target_id}</Typography.Text>
        {(target.restart_required || state?.restart_required) && <Tag color="blue">需要人工重启</Tag>}
      </Space>
      <Typography.Paragraph type="secondary">{status.description}</Typography.Paragraph>
      <Descriptions column={1} size="small">
        <Descriptions.Item label="风险级别">{target.risk_level ?? 'user_config_write'}</Descriptions.Item>
        <Descriptions.Item label="安全说明">{target.description}</Descriptions.Item>
        {baseFingerprint && <Descriptions.Item label="当前基线指纹"><Typography.Text copyable>{baseFingerprint}</Typography.Text></Descriptions.Item>}
      </Descriptions>
      <Typography.Title level={5} className="section-gap">受管逻辑叶子</Typography.Title>
      <List
        size="small"
        className="maintenance-leaf-list"
        dataSource={leaves}
        locale={{ emptyText: '暂无叶子状态' }}
        renderItem={(leaf) => <List.Item><span className="maintenance-leaf-id">{leaf.leaf_id}</span><Space>{leaf.existence && <Tag>{leaf.existence === 'missing' ? '缺失' : '存在'}</Tag>}{leaf.progress && <Tag color="blue">{leaf.progress}</Tag>}</Space></List.Item>}
      />
      <div className="maintenance-preview-action">
        <Button type="primary" ghost disabled={!canPreview || previewLoading} loading={previewLoading} onClick={onPreview}>查看结构化预览</Button>
        {!canPreview && <Typography.Text type="secondary">{inspectionSupported ? '当前状态不具备安全预览条件。' : '当前平台不支持只读检查。'}</Typography.Text>}
      </div>
      {previewError && <Alert className="section-gap" type="error" showIcon message={getPreviewErrorTitle(previewError)} description={getPreviewErrorDescription(previewError)} />}
      {preview && <MaintenancePreview preview={preview} />}
    </Card>
  );
}

/** 只展示目标、步骤、叶子和哈希摘要，不显示完整配置、非受管内容或物理路径。 */
function MaintenancePreview({ preview }: { preview: import('../types/api').MaintenancePreviewData }) {
  const plan = preview.plan;
  return (
    <div className="maintenance-preview-panel">
      <Alert type="info" showIcon message="结构化预览已生成" description="以下内容仅为安全元数据和受管片段摘要；当前没有应用或修复入口。" />
      <Descriptions column={1} size="small" className="section-gap">
        <Descriptions.Item label="前后状态">{getStatusView(preview.inspection.status).label}</Descriptions.Item>
        <Descriptions.Item label="预览摘要"><Typography.Text copyable>{plan.preview_digest}</Typography.Text></Descriptions.Item>
        <Descriptions.Item label="变更前指纹"><Typography.Text copyable>{plan.before_fingerprint}</Typography.Text></Descriptions.Item>
        <Descriptions.Item label="变更后指纹"><Typography.Text copyable>{plan.after_fingerprint}</Typography.Text></Descriptions.Item>
      </Descriptions>
      <Typography.Title level={5} className="section-gap">固定步骤</Typography.Title>
      <Space wrap>{plan.steps.map((step) => <Tag key={step.step_id}>{step.step_id}</Tag>)}</Space>
      <Typography.Title level={5} className="section-gap">受管差异</Typography.Title>
      <List size="small" dataSource={plan.differences ?? []} locale={{ emptyText: '受管内容无变化' }} renderItem={(diff) => <MaintenanceDiff diff={diff} />} />
    </div>
  );
}

function MaintenanceDiff({ diff }: { diff: MaintenanceDiffData }) {
  return <List.Item><div className="maintenance-diff-item"><div><Typography.Text strong>{diff.leaf_id}</Typography.Text><Typography.Text type="secondary" className="maintenance-diff-status">{DIFF_LABELS[diff.difference_code ?? ''] ?? '受管状态已返回'}</Typography.Text></div><Space direction="vertical" size={0} align="end">{diff.before_hash && <Typography.Text type="secondary" className="maintenance-diff-hash">变更前摘要：{diff.before_hash}</Typography.Text>}{diff.after_hash && <Typography.Text type="secondary" className="maintenance-diff-hash">变更后摘要：{diff.after_hash}</Typography.Text>}</Space></div></List.Item>;
}

/** 将完整写能力与只读检查能力分开提示，避免 supported=false 时误报预览不可用。 */
function PlatformCapabilityAlert({ platform }: { platform: import('../types/api').MaintenancePlatformData }) {
  const inspectionSupported = platform.inspection_supported ?? platform.supported;
  const previewSupported = platform.preview_supported ?? inspectionSupported;
  const applySupported = platform.apply_supported ?? platform.supported;
  if (!inspectionSupported) {
    return <Alert className="maintenance-platform-alert" type="warning" showIcon message="当前平台不支持安装与修复检查" description="当前平台没有已验证的只读检查或预览适配器，页面不会尝试读取配置位置。" />;
  }
  return <Alert className="maintenance-platform-alert" type="info" showIcon message={`当前平台：${platform.platform} · 只读检查${previewSupported ? '与预览可用' : '可用'}`} description={applySupported ? '服务端仅返回固定目标和安全元数据。' : '当前仅开放状态查看和结构化预览，安装、修复与写入仍未开放。'} />;
}

function getStatusView(status?: MaintenanceTargetStatus) {
  return STATUS_VIEW[status ?? ''] ?? { label: '状态未知', color: 'default', description: '服务端未返回可识别状态；页面不会猜测或执行修复。' };
}

function getPreviewErrorTitle(error: Error): string {
  const code = 'code' in error ? String((error as Error & { code?: unknown }).code ?? '') : '';
  if (code === 'MAINTENANCE_CONFLICT') return '目标存在冲突';
  if (code === 'MAINTENANCE_TARGET_UNSUPPORTED') return '当前平台不支持';
  return '结构化预览暂时不可用';
}

function getPreviewErrorDescription(error: Error): string {
  const code = 'code' in error ? String((error as Error & { code?: unknown }).code ?? '') : '';
  if (code === 'MAINTENANCE_CONFLICT') return '检测到非受管内容或基线已变化；页面不会覆盖现场，请重新读取状态。';
  if (code === 'MAINTENANCE_TARGET_UNSUPPORTED') return '当前平台没有已验证的维护适配器。';
  return '服务端未能生成只读结构化预览，请稍后重试。';
}
