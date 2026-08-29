/**
 * 知识关系的展示映射只负责把 API 枚举转成用户可读中文。
 * 未知枚举统一降级为安全文案，避免内部关系名直接泄漏到浏览器。
 */
const RELATION_DIRECTION_LABELS: Record<string, string> = {
  incoming: '被关联（传入）',
  outgoing: '关联到（传出）',
};

const RELATION_TYPE_LABELS: Record<string, string> = {
  related_to: '相关',
  depends_on: '依赖',
  implements: '实现',
  supersedes: '替代',
  verified_by: '由其验证',
  applies_to: '适用于',
  part_of: '属于',
};

/** 将关系方向转换为稳定中文；未知或缺失值不回显原始枚举。 */
export function relationDirectionLabel(direction?: string | null): string {
  return direction ? RELATION_DIRECTION_LABELS[direction] ?? '关联方向未说明' : '关联方向未说明';
}

/** 将关系类型转换为稳定中文；未知或未来枚举统一显示为其他关系。 */
export function relationTypeLabel(type?: string | null): string {
  return type ? RELATION_TYPE_LABELS[type] ?? '其他关系' : '其他关系';
}
