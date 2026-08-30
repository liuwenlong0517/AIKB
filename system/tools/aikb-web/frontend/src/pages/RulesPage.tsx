import { Alert, Button, Card, Col, Descriptions, Empty, Row, Space, Tag, Typography } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { AsyncState } from '../components/AsyncState';
import { MarkdownViewer } from '../components/MarkdownViewer';
import { PageHeader } from '../components/PageHeader';
import { usePreviewRule, useRule, useRules } from '../hooks/useApi';
import type { RulePreviewData, RuleSummary, RuleValidation } from '../types/api';

/** 阶段 4A 规则中心：固定目录、正文审阅/编辑和完整预览三栏均不直接接触文件系统。 */
export function RulesPage() {
  const navigate = useNavigate();
  const { ruleId } = useParams<{ ruleId?: string }>();
  const rulesQuery = useRules();
  const detailQuery = useRule(ruleId);
  const previewMutation = usePreviewRule();
  const resetPreviewMutation = previewMutation.reset;
  const [editing, setEditing] = useState(false);
  const [candidate, setCandidate] = useState('');
  const [preview, setPreview] = useState<RulePreviewData>();
  const [previewCreatedAt, setPreviewCreatedAt] = useState<number>();
  const detail = detailQuery.data?.data;
  const summaries = rulesQuery.data?.data.items ?? [];
  const canEdit = detail?.rule_id === 'user' && detail.writable;

  // 路由切换或刷新拿到新正文时，编辑缓冲区必须从服务端基线重新开始。
  useEffect(() => {
    setEditing(false);
    setCandidate(detail?.content ?? '');
    setPreview(undefined);
    setPreviewCreatedAt(undefined);
    resetPreviewMutation();
  }, [detail?.content, resetPreviewMutation, ruleId]);

  // 令牌只在预览内存中短暂展示状态；计时器用于及时阻止过期预览被误认为可用。
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    if (!preview) return undefined;
    const timer = window.setInterval(() => setClock(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [preview]);

  const expired = useMemo(() => {
    if (!preview) return false;
    if (preview.expires_at) return Date.parse(preview.expires_at) <= clock;
    if (preview.expires_in_seconds !== undefined && previewCreatedAt !== undefined) {
      return previewCreatedAt + preview.expires_in_seconds * 1_000 <= clock;
    }
    return false;
  }, [clock, preview, previewCreatedAt]);

  /** 选择规则后保留深层 URL，支持刷新恢复和浏览器前进/后退。 */
  const selectRule = (item: RuleSummary) => navigate(`/rules/${encodeURIComponent(item.rule_id)}`);

  /** 只提交服务端详情中的哈希和受控候选正文；页面没有保存或应用入口。 */
  const requestPreview = () => {
    if (!detail || !canEdit || candidate === detail.content) return;
    setPreview(undefined);
    setPreviewCreatedAt(undefined);
    previewMutation.reset();
    previewMutation.mutate(
      { ruleId: detail.rule_id, base_content_hash: detail.content_hash, candidate_content: candidate },
      {
        onSuccess: (response) => {
          setPreview(response.data);
          setPreviewCreatedAt(Date.now());
        },
      },
    );
  };

  /** 编辑正文时清理旧 diff，避免把不属于当前候选的预览误认为最新结果。 */
  const updateCandidate = (value: string) => {
    setCandidate(value);
    if (preview) {
      setPreview(undefined);
      setPreviewCreatedAt(undefined);
      previewMutation.reset();
    }
  };

  return (
    <>
      <PageHeader
        title="规则中心"
        description="审阅 AIKB 入口与工作协议；预览只校验候选内容，不执行应用操作。"
        extra={<Tag color="blue">阶段 4A · 只预览</Tag>}
      />
      <Row gutter={[16, 16]} className="rules-layout">
        <Col xs={24} lg={7} xl={6}>
          <Card title="规则目录" className="rules-directory-card">
            <AsyncState loading={rulesQuery.isLoading} error={rulesQuery.error} onRetry={() => void rulesQuery.refetch()} empty={!summaries.length} emptyDescription="暂无可读规则">
              <nav aria-label="规则目录" className="rules-directory">
                {summaries.map((item) => (
                  <Link
                    key={item.rule_id}
                    to={`/rules/${encodeURIComponent(item.rule_id)}`}
                    className={`rules-directory-item${item.rule_id === ruleId ? ' is-selected' : ''}`}
                    aria-current={item.rule_id === ruleId ? 'page' : undefined}
                    onClick={(event) => { event.preventDefault(); selectRule(item); }}
                  >
                    <span className="rules-directory-title">{item.title}</span>
                    <span className="rules-directory-id">{item.rule_id}</span>
                    <span className="rules-directory-description">{item.description}</span>
                    <span className="rules-directory-capability">
                      <Tag color={item.rule_id === 'user' && item.writable ? 'orange' : 'default'}>{item.rule_id === 'user' && item.writable ? '可预览修改' : '只读'}</Tag>
                    </span>
                  </Link>
                ))}
              </nav>
            </AsyncState>
          </Card>
        </Col>

        <Col xs={24} lg={10} xl={10}>
          <Card title={detail?.title ?? '正文审阅或编辑'} className="rules-content-card">
            {!ruleId ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="从左侧选择一项规则" />
            ) : (
              <AsyncState loading={detailQuery.isLoading} error={detailQuery.error} onRetry={() => void detailQuery.refetch()} empty={!detail} emptyDescription="未找到该规则">
                {detail && <>
                  <Space wrap className="rules-content-actions">
                    <Tag color={canEdit ? 'orange' : 'default'}>{canEdit ? '唯一可编辑规则' : '只读规则'}</Tag>
                    <Typography.Text type="secondary">最多 {detail.max_chars.toLocaleString()} 字符</Typography.Text>
                    {canEdit && !editing && <Button onClick={() => setEditing(true)}>编辑正文</Button>}
                    {canEdit && editing && <Button onClick={() => { setEditing(false); setCandidate(detail.content); setPreview(undefined); previewMutation.reset(); }}>取消编辑</Button>}
                    {canEdit && editing && <Button type="primary" disabled={!candidate || candidate === detail.content} loading={previewMutation.isPending} onClick={requestPreview}>生成完整预览</Button>}
                  </Space>
                  {canEdit && <Alert className="section-gap" type="warning" showIcon message="修改只影响新会话，已运行 Agent 不会自动重载。" description="本页面仅生成候选校验和差异；当前批次不提供保存、应用或确认执行按钮。" />}
                  {editing && canEdit ? (
                    <textarea
                      aria-label="规则正文编辑器"
                      className="rules-editor"
                      value={candidate}
                      onChange={(event) => updateCandidate(event.target.value)}
                      spellCheck={false}
                    />
                  ) : <MarkdownViewer content={detail.content} />}
                  <Descriptions column={1} size="small" className="section-gap">
                    <Descriptions.Item label="规则 ID">{detail.rule_id}</Descriptions.Item>
                    <Descriptions.Item label="最近 revision"><Typography.Text copyable>{detail.revision}</Typography.Text></Descriptions.Item>
                    <Descriptions.Item label="当前内容哈希"><Typography.Text copyable>{detail.content_hash}</Typography.Text></Descriptions.Item>
                  </Descriptions>
                </>}
              </AsyncState>
            )}
          </Card>
        </Col>

        <Col xs={24} lg={7} xl={8}>
          <PreviewPanel preview={preview} expired={expired} loading={previewMutation.isPending} error={previewMutation.error} />
        </Col>
      </Row>
    </>
  );
}

interface PreviewPanelProps {
  preview?: RulePreviewData;
  expired: boolean;
  loading: boolean;
  error: Error | null;
}

/** 预览面板完整展示 diff 与校验结果；不提供确认/应用按钮，避免越过本批次边界。 */
function PreviewPanel({ preview, expired, loading, error }: PreviewPanelProps) {
  const diff = preview?.unified_diff ?? preview?.diff ?? '';
  const validation = preview?.validation;
  const validationState = validation ? getValidationState(validation) : undefined;
  return (
    <Card title="差异和校验" className="rules-preview-card">
      {loading && <div className="rules-preview-loading">正在生成完整差异和候选校验…</div>}
        {error && <Alert type="error" showIcon message={getPreviewErrorTitle()} description={getPreviewErrorDescription(error)} />}
      {!loading && !error && !preview && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="编辑 user 规则后生成预览" />}
      {preview && <div className="rules-preview-body">
        {expired && <Alert type="warning" showIcon message="预览已过期" description="请返回正文重新生成预览；过期令牌不会执行任何写入。" />}
        {!expired && <Alert type="info" showIcon message="预览已生成" description="差异已完整展示。当前批次禁止应用或确认执行。" />}
        {validationState && <Alert className="section-gap" type={validationState.type} showIcon message={validationState.message} description={validationState.description} />}
        <Descriptions column={1} size="small" className="section-gap">
          <Descriptions.Item label="变更 ID">{preview.change_id}</Descriptions.Item>
          <Descriptions.Item label="预览摘要"><Typography.Text copyable>{preview.preview_digest}</Typography.Text></Descriptions.Item>
          {preview.before_hash && <Descriptions.Item label="变更前哈希">{preview.before_hash}</Descriptions.Item>}
          {preview.after_hash && <Descriptions.Item label="候选哈希">{preview.after_hash}</Descriptions.Item>}
          {preview.expires_at && <Descriptions.Item label="有效至">{preview.expires_at}</Descriptions.Item>}
        </Descriptions>
        <Typography.Title level={5} className="section-gap">完整 unified diff</Typography.Title>
        <pre data-testid="rule-unified-diff" className="rules-diff" tabIndex={0}>{diff || '候选与当前正文没有差异。'}</pre>
        <Typography.Text type="secondary" className="rules-token-note">服务端确认凭据已保留在当前预览状态中；本批次不展示或消费该凭据。</Typography.Text>
      </div>}
    </Card>
  );
}

/** 将不同校验器版本的安全摘要压缩为统一的页面提示，不回显候选正文。 */
function getValidationState(validation: RuleValidation): { type: 'success' | 'warning' | 'error'; message: string; description: string } {
  const valid = validation.valid ?? validation.ok;
  const errors = formatIssues(validation.errors);
  const warnings = formatIssues(validation.warnings);
  if (valid === false || errors.length) return { type: 'error', message: '候选校验未通过', description: errors.join('；') || '服务端拒绝该候选内容。' };
  if (warnings.length) return { type: 'warning', message: '候选通过，但存在提示', description: warnings.join('；') };
  if (valid === true) return { type: 'success', message: '候选校验通过', description: '候选内容满足服务端规则约束。' };
  return { type: 'warning', message: '已返回校验结果', description: '请查看服务端提供的校验摘要。' };
}

/** 仅呈现服务端提供的字段级问题文本，避免把任意对象序列化到页面。 */
function formatIssues(issues: RuleValidation['errors']): string[] {
  return (issues ?? []).map((issue) => typeof issue === 'string' ? issue : issue.message ?? issue.code ?? '校验问题');
}

/** 将冲突/过期等预览错误映射为用户可操作的提示。 */
function getPreviewErrorTitle(): string {
  return '预览失败';
}

function getPreviewErrorDescription(error: Error): string {
  const code = 'code' in error ? String((error as Error & { code?: unknown }).code ?? '') : '';
  if (code === 'base_hash_conflict' || code === 'conflict' || code === 'revision_conflict' || /409|冲突|基线/.test(error.message)) return '规则正文或仓库基线已变化，请刷新详情后重新编辑和预览。';
  if (code === 'preview_token_expired' || code === 'expired' || /过期/.test(error.message)) return '当前预览已过期，请重新生成候选预览。';
  const details = 'details' in error ? (error as Error & { details?: unknown }).details : undefined;
  if (details && typeof details === 'object' && 'validation' in details) {
    const validation = details.validation;
    if (validation && typeof validation === 'object' && 'errors' in validation) {
      const issues = formatIssues((validation as RuleValidation).errors);
      if (issues.length) return `候选校验未通过：${issues.join('；')}`;
    }
  }
  return error.message;
}
