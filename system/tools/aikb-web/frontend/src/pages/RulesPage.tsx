import { Alert, Button, Card, Checkbox, Col, Descriptions, Empty, Row, Space, Tag, Typography } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { AsyncState } from '../components/AsyncState';
import { MarkdownViewer } from '../components/MarkdownViewer';
import { PageHeader } from '../components/PageHeader';
import { useApplyRule, usePreviewRule, useRule, useRuleChange, useRules } from '../hooks/useApi';
import type { RuleApplyData, RuleChangeData, RulePreviewData, RuleSummary, RuleValidation } from '../types/api';

/** 阶段 4A 规则中心：固定目录、正文审阅/编辑和完整预览三栏均不直接接触文件系统。 */
export function RulesPage() {
  const navigate = useNavigate();
  const { ruleId } = useParams<{ ruleId?: string }>();
  const rulesQuery = useRules();
  const detailQuery = useRule(ruleId);
  const previewMutation = usePreviewRule();
  const applyMutation = useApplyRule();
  const resetPreviewMutation = previewMutation.reset;
  const resetApplyMutation = applyMutation.reset;
  const [editing, setEditing] = useState(false);
  const [candidate, setCandidate] = useState('');
  const [preview, setPreview] = useState<RulePreviewData>();
  const [previewCreatedAt, setPreviewCreatedAt] = useState<number>();
  const [confirmed, setConfirmed] = useState(false);
  const [applySubmitted, setApplySubmitted] = useState(false);
  const [applyResult, setApplyResult] = useState<RuleApplyData>();
  const detail = detailQuery.data?.data;
  const refetchDetail = detailQuery.refetch;
  const summaries = rulesQuery.data?.data.items ?? [];
  const canEdit = detail?.rule_id === 'user' && detail.writable;
  const appliedChangeId = applyResult?.change_id;
  const changeQuery = useRuleChange(appliedChangeId, Boolean(appliedChangeId));
  const changeResponse = changeQuery.data?.data;
  const change = changeResponse?.change;
  const changeId = change?.change_id;
  const changeStatus = change?.status;
  const terminalRefreshKey = useRef<string>();

  // 路由切换或刷新进入新规则时，清空旧预览/令牌；不会因刷新自动重放 apply。
  useEffect(() => {
    setEditing(false);
    setCandidate('');
    setPreview(undefined);
    setPreviewCreatedAt(undefined);
    setConfirmed(false);
    setApplySubmitted(false);
    setApplyResult(undefined);
    terminalRefreshKey.current = undefined;
    resetPreviewMutation();
    resetApplyMutation();
  }, [resetApplyMutation, resetPreviewMutation, ruleId]);

  // 详情内容变化（包括应用后的服务端刷新）重新建立编辑基线，但不覆盖任务结果。
  useEffect(() => {
    setCandidate(detail?.content ?? '');
  }, [detail?.content]);

  // 事务进入成功或已回滚终态后重读正式正文；旧 diff/令牌同时失效。
  useEffect(() => {
    const refreshKey = changeId && changeStatus ? `${changeId}:${changeStatus}` : undefined;
    if (!changeStatus || !refreshKey || !['succeeded', 'rolled_back'].includes(changeStatus) || terminalRefreshKey.current === refreshKey) return;
    terminalRefreshKey.current = refreshKey;
    setPreview(undefined);
    setPreviewCreatedAt(undefined);
    setConfirmed(false);
    setEditing(false);
    resetPreviewMutation();
    void refetchDetail();
  }, [changeId, changeStatus, refetchDetail, resetPreviewMutation]);

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
    setConfirmed(false);
    setApplySubmitted(false);
    setApplyResult(undefined);
    previewMutation.reset();
    applyMutation.reset();
    previewMutation.mutate(
      { ruleId: detail.rule_id, base_content_hash: detail.content_hash, candidate_content: candidate },
      {
        onSuccess: (response) => {
          setPreview(response.data);
          setPreviewCreatedAt(Date.now());
          setConfirmed(false);
          setApplySubmitted(false);
          setApplyResult(undefined);
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
      setConfirmed(false);
      setApplySubmitted(false);
      setApplyResult(undefined);
      previewMutation.reset();
      applyMutation.reset();
    }
  };

  /** 仅在完整、未过期预览上提交 change_id 与短期令牌；正文和路径永不进入 apply 请求。 */
  const submitApply = () => {
    if (!detail || !preview || expired || !preview.confirmation_token || !confirmed || applySubmitted) return;
    setApplySubmitted(true);
    applyMutation.mutate(
      {
        ruleId: detail.rule_id,
        change_id: preview.change_id,
        confirmation_token: preview.confirmation_token,
      },
      { onSuccess: (response) => setApplyResult(response.data) },
    );
  };

  /** 失败后重新读取正式正文并清除旧令牌；用户必须重新编辑和生成预览，页面不自动重放。 */
  const restartAfterApplyFailure = () => {
    setPreview(undefined);
    setPreviewCreatedAt(undefined);
    setConfirmed(false);
    setApplySubmitted(false);
    setApplyResult(undefined);
    setEditing(false);
    setCandidate('');
    previewMutation.reset();
    applyMutation.reset();
    void refetchDetail();
  };

  return (
    <>
      <PageHeader
        title="规则中心"
        description="审阅 AIKB 入口与工作协议；user 规则支持先预览、再受控应用。"
        extra={<Tag color="blue">阶段 4A · 受控变更</Tag>}
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
                    <Typography.Text type="secondary">
                      建议 {(detail.recommended_chars ?? detail.max_chars).toLocaleString()} / 最多 {detail.max_chars.toLocaleString()} 字符
                    </Typography.Text>
                    {canEdit && !editing && <Button onClick={() => setEditing(true)}>编辑正文</Button>}
                    {canEdit && editing && <Button onClick={() => { setEditing(false); setCandidate(detail.content); setPreview(undefined); previewMutation.reset(); }}>取消编辑</Button>}
                    {canEdit && editing && <Button type="primary" disabled={!candidate || candidate === detail.content} loading={previewMutation.isPending} onClick={requestPreview}>生成完整预览</Button>}
                  </Space>
                  {canEdit && <Alert className="section-gap" type="warning" showIcon message="修改只影响新会话，已运行 Agent 不会自动重载。" description="正式写入必须先完成右侧完整 diff 审阅和高风险确认；不会产生 Git 提交。" />}
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
          <PreviewPanel
            preview={preview}
            expired={expired}
            loading={previewMutation.isPending}
            error={previewMutation.error}
            confirmed={confirmed}
            canApply={Boolean(preview && !expired && preview.confirmation_token && preview.validation?.valid !== false && !applySubmitted)}
            applying={applyMutation.isPending}
            applySubmitted={applySubmitted}
            applyError={applyMutation.error}
            applyResult={applyResult}
            change={change}
            changeTaskId={changeResponse?.task?.task_id}
            changeBlocked={changeResponse?.blocked}
            changeLoading={changeQuery.isLoading}
            changeError={changeQuery.error}
            onConfirmChange={setConfirmed}
            onApply={submitApply}
            onRestart={restartAfterApplyFailure}
          />
        </Col>
      </Row>
    </>
  );
}

/** 预览面板完整展示 diff 与校验结果，并在成功且未过期时进入高风险确认区。 */
function PreviewPanel({ preview, expired, loading, error, confirmed, canApply, applying, applySubmitted, applyError, applyResult, change, changeTaskId, changeBlocked, changeLoading, changeError, onConfirmChange, onApply, onRestart }: PreviewPanelProps) {
  const diff = preview?.unified_diff ?? preview?.diff ?? '';
  const validation = preview?.validation;
  const validationState = validation ? getValidationState(validation) : undefined;
  const status = change?.status ?? applyResult?.status;
  const taskId = change?.task_id ?? applyResult?.task_id ?? applyResult?.task?.task_id;
  return (
    <Card title="差异和校验" className="rules-preview-card">
      {loading && <div className="rules-preview-loading">正在生成完整差异和候选校验…</div>}
        {error && <Alert type="error" showIcon message={getPreviewErrorTitle()} description={getPreviewErrorDescription(error)} />}
      {!loading && !error && !preview && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="编辑 user 规则后生成预览" />}
      {preview && <div className="rules-preview-body">
        {expired && <Alert type="warning" showIcon message="预览已过期" description="请返回正文重新生成预览；过期令牌不会执行任何写入。" />}
        {!expired && <Alert type="info" showIcon message="预览已生成" description="请完整审阅差异和校验结果；确认区只会提交服务端生成的变更摘要。" />}
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
        {canApply && <div className="rules-confirmation-zone">
          <Alert type="warning" showIcon message="高风险确认：即将正式写入 USER_RULES.md" description="应用会更新控制仓规则文件，仅影响新会话；已运行 Agent 不会自动重载，也不会产生 Git 提交。" />
          <Checkbox checked={confirmed} onChange={(event) => onConfirmChange(event.target.checked)} className="rules-confirmation-checkbox">
            我已完整审阅 unified diff，并确认正式写入 USER_RULES.md；该操作影响新会话且不产生 Git 提交。
          </Checkbox>
          <Button danger type="primary" disabled={!confirmed || applySubmitted} loading={applying} onClick={onApply}>
            {applySubmitted ? '已提交，等待结果' : '确认并提交规则应用'}
          </Button>
        </div>}
        {applyError && <Alert className="section-gap" type="error" showIcon message={getApplyErrorTitle(applyError)} description={getApplyErrorDescription(applyError)} action={<Button onClick={onRestart}>重新读取并生成新预览</Button>} />}
      </div>}
      {applyResult && <ApplyResultPanel result={applyResult} status={status} taskId={taskId ?? changeTaskId} blocked={changeBlocked} change={change} loading={changeLoading} error={changeError} />}
      {(preview || applyResult) && <Typography.Text type="secondary" className="rules-token-note">确认凭据仅保留在当前页面内存，apply 请求不携带规则正文或路径；刷新页面不会自动重放。</Typography.Text>}
    </Card>
  );
}

interface PreviewPanelProps {
  preview?: RulePreviewData;
  expired: boolean;
  loading: boolean;
  error: Error | null;
  confirmed: boolean;
  canApply: boolean;
  applying: boolean;
  applySubmitted: boolean;
  applyError: Error | null;
  applyResult?: RuleApplyData;
  change?: RuleChangeData;
  changeTaskId?: string;
  changeBlocked?: boolean;
  changeLoading: boolean;
  changeError: Error | null;
  onConfirmChange: (checked: boolean) => void;
  onApply: () => void;
  onRestart: () => void;
}

/** 展示 apply 返回的任务关联和事务终态，失败恢复状态只使用服务端安全枚举。 */
function ApplyResultPanel({ result, status, taskId, blocked, change, loading, error }: { result: RuleApplyData; status?: string; taskId?: string | null; blocked?: boolean; change?: RuleChangeData; loading: boolean; error: Error | null }) {
  if (loading && !status && !error) return <div className="rules-preview-loading">正在读取规则应用状态…</div>;
  const statusView = ruleChangeStatusView(status);
  return <div className="rules-apply-result section-gap">
    {error && <Alert type="warning" showIcon message="正在读取规则变更状态" description="任务已提交，但当前无法读取最新状态；下方仍保留任务和变更安全摘要。" />}
    {blocked && <Alert type="error" showIcon message="规则写入已被阻断" description="系统要求人工检查恢复状态；页面不会自动重试或再次提交。" />}
    <Alert type={statusView.type} showIcon message={statusView.title} description={statusView.description} />
    <Descriptions column={1} size="small" className="section-gap">
      <Descriptions.Item label="变更 ID">{result.change_id}</Descriptions.Item>
      {taskId && <Descriptions.Item label="任务 ID">{taskId}</Descriptions.Item>}
      {change?.rollback_status && <Descriptions.Item label="回滚状态">{change.rollback_status}</Descriptions.Item>}
    </Descriptions>
    {taskId && <Button type="link"><Link to={`/tasks/${encodeURIComponent(taskId)}`}>查看任务中心</Link></Button>}
  </div>;
}

/** 把事务状态机的固定枚举映射为用户可操作的安全摘要，不猜测底层异常。 */
function ruleChangeStatusView(status?: string): { type: 'success' | 'info' | 'warning' | 'error'; title: string; description: string } {
  switch (status) {
    case 'succeeded': return { type: 'success', title: '规则已成功应用', description: 'USER_RULES.md 已完成受控更新；修改仅对新会话生效。' };
    case 'rolled_back': return { type: 'warning', title: '应用失败，已成功回滚', description: '正式规则已恢复到应用前内容，请检查任务中心和审计记录。' };
    case 'recovery_required': return { type: 'error', title: '需要人工恢复', description: '系统检测到无法自动完成恢复，请根据系统状态和审计安全摘要人工处理。' };
    case 'expired': return { type: 'warning', title: '规则预览已过期', description: '请重新读取正文并生成预览，旧令牌不会再次提交。' };
    case 'rejected': return { type: 'error', title: '规则应用被拒绝', description: '服务端拒绝了该规则事务，请重新读取正文和预览。' };
    case 'applying': return { type: 'info', title: '正在应用规则', description: '受控任务正在写入 USER_RULES.md。' };
    case 'validating': return { type: 'info', title: '正在复核规则', description: '正在运行正式文件校验，页面不会重复提交。' };
    case 'rolling_back': return { type: 'warning', title: '正在回滚规则', description: '应用复核未完成，系统正在恢复应用前内容。' };
    default: return { type: 'info', title: '规则任务已提交', description: '任务和变更事务已关联，正在等待服务端状态。' };
  }
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

/** 将 apply 的安全错误码映射为不泄露正文/路径的处理提示。 */
function getApplyErrorTitle(error: Error): string {
  const code = 'code' in error ? String((error as Error & { code?: unknown }).code ?? '') : '';
  if (code === 'base_hash_conflict' || code === 'revision_conflict' || code === 'rule_change_conflict') return '规则基线已冲突';
  if (code.includes('token') || code === 'preview_expired' || code === 'rule_confirmation_invalid') return '预览令牌已过期或失效';
  if (code === 'audit_write_failed' || code === 'audit_unavailable') return '审计关联失败';
  return '规则应用失败';
}

function getApplyErrorDescription(error: Error): string {
  const code = 'code' in error ? String((error as Error & { code?: unknown }).code ?? '') : '';
  if (code === 'base_hash_conflict' || code === 'revision_conflict' || code === 'rule_change_conflict' || /冲突|基线/.test(error.message)) return '规则正文、仓库 revision 或变更事务已变化，请重新读取正文并生成预览。';
  if (code.includes('token') || code === 'preview_expired' || code === 'rule_confirmation_invalid' || /过期|令牌/.test(error.message)) return '当前凭据不可重用，请重新读取正文并生成新的完整预览。';
  if (code === 'audit_write_failed' || code === 'audit_unavailable') return '审计开始事实未成功关联，系统未确认写入结果；请查看审计和系统状态后再处理。';
  return error.message;
}
