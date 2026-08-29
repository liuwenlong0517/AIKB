import ReactMarkdown from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';

/**
 * 只读 Markdown 阅读器。
 * 采用 GFM + sanitize，且不启用 rehypeRaw：文档中的 HTML 不会被当作 React/DOM 代码执行。
 */
export function MarkdownViewer({ content }: { content: string }) {
  return (
    <article className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        components={{
          // 外链在新标签页打开并明确 rel，避免文档内容反向控制当前管理终端。
          a: ({ href, children }) => {
            const sanitizedHref = safeHref(href);
            return sanitizedHref ? (
              <a href={sanitizedHref} target="_blank" rel="noreferrer noopener">{children}</a>
            ) : <span>{children}</span>;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </article>
  );
}

/** 只放行明确的外链协议；相对链接按当前 WebUI origin 解析后也可安全打开。 */
function safeHref(href: string | undefined) {
  if (!href) return undefined;
  try {
    const parsed = new URL(href, window.location.origin);
    return ['http:', 'https:', 'mailto:'].includes(parsed.protocol) ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}
