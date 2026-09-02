import { Alert, Button, Card, Checkbox, Col, Descriptions, InputNumber, List, Row, Space, Statistic, Tag, Typography } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { AsyncState } from '../components/AsyncState';
import { PageHeader } from '../components/PageHeader';
import { useApplyDataMaintenance, useDataMaintenanceOverview, usePreviewDataMaintenance } from '../hooks/useApi';
import type { DataMaintenanceCategory, DataMaintenancePreview } from '../types/api';

const CATEGORY_IDS: DataMaintenanceCategory['id'][] = ['audit', 'archived_work', 'web_tasks'];
const PROTECTION_LABELS: Record<string, string> = {
  within_retention: '仍在保留期内',
  uncertain_or_active: '活动中或状态无法安全确认',
  unreadable: '当前无法安全读取',
  unsafe_object: '包含链接或未知对象',
};

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

/** 数据维护页只组织固定类别、保留期和服务端计划，不生成或持久化路径。 */
export function DataMaintenancePage() {
  const overviewQuery = useDataMaintenanceOverview();
  const previewMutation = usePreviewDataMaintenance();
  const applyMutation = useApplyDataMaintenance();
  const [selected, setSelected] = useState<DataMaintenanceCategory['id'][]>(CATEGORY_IDS);
  const [retention, setRetention] = useState<Record<string, number>>({});
  const [preview, setPreview] = useState<DataMaintenancePreview>();
  const [confirmed, setConfirmed] = useState(false);
  const overview = overviewQuery.data?.data;

  useEffect(() => {
    if (overview && Object.keys(retention).length === 0) setRetention(overview.defaults);
  }, [overview, retention]);

  const categories = useMemo(() => overview?.categories ?? [], [overview]);
  const requestPreview = () => {
    if (!selected.length) return;
    setPreview(undefined); setConfirmed(false); applyMutation.reset();
    previewMutation.mutate({ categories: selected, retention_days: Object.fromEntries(selected.map((id) => [id, retention[id] ?? overview?.defaults[id] ?? 1])) }, { onSuccess: (response) => setPreview(response.data) });
  };
  const apply = () => {
    if (!preview || !confirmed || applyMutation.isPending) return;
    applyMutation.mutate({ planId: preview.plan_id, confirmation_token: preview.confirmation_token }, { onSuccess: () => { setConfirmed(false); setPreview(undefined); } });
  };

  return <>
    <PageHeader title="数据维护" description="盘点并清理超过保留期的本机历史数据；活动状态和不确定对象始终受到保护。" extra={<Tag color="red">高风险 · 删除本机数据</Tag>} />
    <Alert type="warning" showIcon message="清理不可撤销，必须先生成最新预览" description="页面只允许选择服务端固定类别和保留天数，不接受路径；执行前会重新扫描，任何候选变化都会拒绝删除。" />
    <AsyncState loading={overviewQuery.isLoading} error={overviewQuery.error} empty={!overview} onRetry={() => void overviewQuery.refetch()}>
      <Row gutter={[16, 16]} className="section-gap">
        {categories.map((category) => <Col xs={24} lg={8} key={category.id}>
          <Card className="data-maintenance-category-card" title={<Checkbox checked={selected.includes(category.id)} onChange={(event) => { setSelected((current) => event.target.checked ? [...current, category.id] : current.filter((id) => id !== category.id)); setPreview(undefined); setConfirmed(false); }}>{category.label}</Checkbox>}>
            <Space direction="vertical" size="middle" className="data-maintenance-category-content">
              <Space><Typography.Text>保留</Typography.Text><InputNumber aria-label={`${category.label}保留天数`} min={1} max={36500} value={retention[category.id] ?? category.retention_days} onChange={(value) => { setRetention((current) => ({ ...current, [category.id]: Number(value ?? category.retention_days) })); setPreview(undefined); setConfirmed(false); }} /><Typography.Text>天</Typography.Text></Space>
              <Row gutter={12}><Col span={12}><Statistic title="可清理" value={category.candidate_count} suffix="项" /></Col><Col span={12}><Statistic title="预计释放" value={formatBytes(category.candidate_bytes)} /></Col></Row>
              <Typography.Text type="secondary">当前保护 {category.protected_count} 项</Typography.Text>
            </Space>
          </Card>
        </Col>)}
      </Row>
      {overview?.protected.count ? <Card title="保护摘要" className="section-gap"><List size="small" dataSource={overview.protected.reasons} renderItem={(item) => <List.Item><span>{PROTECTION_LABELS[item.code] ?? '受安全策略保护'}</span><Tag>{item.count} 项</Tag></List.Item>} /></Card> : null}
      <Button className="section-gap" type="primary" disabled={!selected.length} loading={previewMutation.isPending} onClick={requestPreview}>生成清理预览</Button>
      {previewMutation.error && <Alert className="section-gap" type="error" showIcon message="无法生成清理预览" description={previewMutation.error.message} />}
      {preview && <CleanupPreviewPanel preview={preview} confirmed={confirmed} applying={applyMutation.isPending} onConfirmed={setConfirmed} onApply={apply} />}
      {applyMutation.error && <Alert className="section-gap" type="error" showIcon message="清理未执行" description={applyMutation.error.message} />}
      {applyMutation.data && <Alert className="section-gap" type="success" showIcon message="数据维护完成" description={`已清理 ${applyMutation.data.data.deleted_count} 项，释放 ${formatBytes(applyMutation.data.data.deleted_bytes)}。`} />}
    </AsyncState>
  </>;
}

function CleanupPreviewPanel({ preview, confirmed, applying, onConfirmed, onApply }: { preview: DataMaintenancePreview; confirmed: boolean; applying: boolean; onConfirmed: (value: boolean) => void; onApply: () => void }) {
  return <Card className="section-gap data-maintenance-preview" title="清理预览">
    <Alert type={preview.candidate_count ? 'warning' : 'info'} showIcon message={preview.candidate_count ? `将永久删除 ${preview.candidate_count} 项历史数据` : '当前没有符合条件的过期数据'} description={`预计释放 ${formatBytes(preview.candidate_bytes)}；有效至 ${new Date(preview.expires_at).toLocaleString('zh-CN')}。`} />
    <Descriptions className="section-gap" column={1} size="small"><Descriptions.Item label="计划 ID">{preview.plan_id}</Descriptions.Item><Descriptions.Item label="风险级别">删除本机历史数据</Descriptions.Item></Descriptions>
    <List size="small" dataSource={preview.categories} renderItem={(item) => <List.Item><span>{item.label}（保留 {item.retention_days} 天）</span><Space><Tag>{item.candidate_count} 项</Tag><Typography.Text type="secondary">{formatBytes(item.candidate_bytes)}</Typography.Text></Space></List.Item>} />
    {preview.candidate_count > 0 && <div className="data-maintenance-confirmation"><Checkbox checked={confirmed} onChange={(event) => onConfirmed(event.target.checked)}>我已核对类别、保留期和候选数量，确认永久删除本次预览中的过期数据。</Checkbox><Button danger type="primary" disabled={!confirmed} loading={applying} onClick={onApply}>确认并执行清理</Button></div>}
  </Card>;
}
