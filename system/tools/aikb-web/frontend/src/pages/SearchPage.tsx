import { useEffect, useState } from 'react';
import { Alert, Button, Card, Empty, Input, List, Select, Space, Tag, Typography } from 'antd';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { AsyncState } from '../components/AsyncState';
import { PageHeader } from '../components/PageHeader';
import { useOverview, useSearch } from '../hooks/useApi';
import type { SearchFilters } from '../types/api';

const TYPE_LABELS: Record<string, string> = {
  decision: '工程决策',
  knowledge: '通用知识',
  pitfall: '工程陷阱',
  'project-memory': '项目知识',
  solution: '解决方案',
  workflow: '工作流',
};

/** 搜索参数保留为语义筛选条件，交由后端索引实现，避免前端复制搜索算法。 */
export function SearchPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const effectiveQuery = searchParams.get('q') ?? '';
  const filters: SearchFilters = {};
  const typeFilter = searchParams.get('type');
  const tagFilter = searchParams.get('tag');
  if (typeFilter) filters.type = typeFilter;
  if (tagFilter) filters.tag = tagFilter;
  const [input, setInput] = useState(effectiveQuery);
  useEffect(() => setInput(effectiveQuery), [effectiveQuery]);
  const overview = useOverview();
  const query = useSearch(effectiveQuery, filters);
  const updateUrl = (next: { q?: string; type?: string; tag?: string }) => {
    const params = new URLSearchParams();
    if (next.q) params.set('q', next.q);
    if (next.type) params.set('type', next.type);
    if (next.tag) params.set('tag', next.tag);
    const queryString = params.toString();
    navigate(`/search${queryString ? `?${queryString}` : ''}`);
  };
  const submit = () => updateUrl({ q: input.trim() || undefined, type: filters.type, tag: filters.tag });
  const updateFilter = (key: keyof SearchFilters, value?: string) => {
    // 空值统一转换为 undefined，避免把“清空”误传给后端成为一个实际筛选词。
    updateUrl({ q: effectiveQuery || undefined, type: key === 'type' ? value : filters.type, tag: key === 'tag' ? value : filters.tag });
  };
  const typeOptions = Object.entries(overview.data?.by_type ?? {})
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([value, count]) => ({ value, label: `${TYPE_LABELS[value] ?? value}（${count}篇）` }));
  const tagOptions = (overview.data?.by_tag ?? []).map(({ tag, count }) => ({ value: tag, label: `${tag}（${count}篇）` }));

  return (
    <>
      <PageHeader title="搜索" description="全文搜索正式知识，并按类型和标签筛选。" />
      <Card className="search-controls">
        <Space.Compact block>
          <Input aria-label="搜索关键词" value={input} onChange={(event) => setInput(event.target.value)} onPressEnter={submit} placeholder="输入关键词…" />
          <Button type="primary" onClick={submit}>搜索</Button>
        </Space.Compact>
        <Space wrap className="filter-row">
          <Select
            allowClear
            aria-label="按知识类型筛选"
            showSearch
            optionFilterProp="label"
            placeholder="全部知识类型"
            loading={overview.isLoading}
            disabled={overview.isError}
            options={typeOptions}
            style={{ width: 220 }}
            value={filters.type}
            onChange={(value) => updateFilter('type', value)}
          />
          <Select
            allowClear
            aria-label="按知识标签筛选"
            showSearch
            optionFilterProp="label"
            placeholder="全部知识标签"
            loading={overview.isLoading}
            disabled={overview.isError}
            options={tagOptions}
            style={{ width: 220 }}
            value={filters.tag}
            onChange={(value) => updateFilter('tag', value)}
          />
          <Button disabled={!filters.type && !filters.tag} onClick={() => updateUrl({ q: effectiveQuery || undefined })}>清除筛选</Button>
        </Space>
        <Alert
          type="info"
          showIcon
          message="筛选条件怎么用？"
          description="请先输入关键词，类型和标签只用于缩小关键词搜索结果。两个条件均来自知识库元数据，同时选择时取交集；清空某一项表示不限制该条件。选项后的篇数是当前知识库中的文档数量。"
          style={{ marginTop: 16 }}
        />
      </Card>
      {!effectiveQuery.trim() ? <Empty className="search-empty" description="输入关键词开始搜索" /> : (
        <AsyncState loading={query.isLoading} error={query.error} onRetry={() => void query.refetch()} empty={!query.data?.results.length} emptyDescription="没有找到匹配的知识文档">
          <Card title={<span>搜索结果 <Typography.Text type="secondary">{query.data?.count ?? 0} 条</Typography.Text></span>} className="section-gap">
            <List itemLayout="vertical" dataSource={query.data?.results ?? []} renderItem={(item) => (
              <List.Item actions={[<Button type="link" key="open" onClick={() => navigate(`/knowledge/view?id=${encodeURIComponent(item.id)}`)}>打开文档</Button>]}>
                <List.Item.Meta title={item.title} description={<Space wrap><Tag>{item.type}</Tag><Typography.Text type="secondary">{item.path}</Typography.Text></Space>} />
                <Typography.Paragraph ellipsis={{ rows: 2 }}>{item.excerpt ?? '后端未提供预览片段。'}</Typography.Paragraph>
              </List.Item>
            )} />
          </Card>
        </AsyncState>
      )}
    </>
  );
}
