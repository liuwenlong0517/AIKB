import { Card, Col, Empty, Row, Tree, Typography } from 'antd';
import type { DataNode, TreeProps } from 'antd/es/tree';
import { useNavigate } from 'react-router-dom';
import { AsyncState } from '../components/AsyncState';
import { PageHeader } from '../components/PageHeader';
import { useKnowledgeTree } from '../hooks/useApi';
import type { TreeNode } from '../types/api';

const TREE_VIEWPORT_HEIGHT = 480;

/** 知识目录只读取逻辑路径；点击文档通过后端的稳定 id 打开阅读页。 */
export function KnowledgePage() {
  const navigate = useNavigate();
  const query = useKnowledgeTree();
  const root = query.data;
  const onSelect: TreeProps['onSelect'] = (selectedKeys, info) => {
    if (info.node.isLeaf && selectedKeys[0]) navigate(`/knowledge/view?id=${encodeURIComponent(String(selectedKeys[0]))}`);
  };
  return (
    <>
      <PageHeader title="知识库" description="按目录浏览 Git + Markdown 事实源。内容在此阶段严格只读。" />
      <AsyncState loading={query.isLoading} error={query.error} onRetry={() => void query.refetch()} empty={!root?.children?.length} emptyDescription="知识目录为空">
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={9} xl={8}>
            <Card title="知识目录" className="tree-card">
              <Tree
                blockNode
                showLine
                defaultExpandAll
                virtual
                height={TREE_VIEWPORT_HEIGHT}
                treeData={root ? [toDataNode(root)] : []}
                onSelect={onSelect}
              />
            </Card>
          </Col>
          <Col xs={24} lg={15} xl={16}>
            <Card className="knowledge-intro">
              <Typography.Title level={3}>选择一篇知识文档</Typography.Title>
              <Typography.Paragraph type="secondary">目录来自后端知识查询服务。浏览器不会直接读取本地文件、SQLite 或 Git。</Typography.Paragraph>
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="从左侧目录打开 Markdown 阅读页" />
            </Card>
          </Col>
        </Row>
      </AsyncState>
    </>
  );
}

function toDataNode(node: TreeNode): DataNode {
  const isLeaf = node.kind === 'document';
  return {
    key: isLeaf ? node.id ?? node.path : node.path,
    title: isLeaf ? node.title ?? node.name : node.name,
    isLeaf,
    children: node.children?.map(toDataNode),
  };
}
