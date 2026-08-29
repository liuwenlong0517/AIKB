import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MarkdownViewer } from '../src/components/MarkdownViewer';

describe('MarkdownViewer', () => {
  it('renders GFM markdown while removing executable HTML', () => {
    render(<MarkdownViewer content={'# 标题\n\n<script>alert("x")</script>\n\n**安全**'} />);
    expect(screen.getByRole('heading', { name: '标题' })).toBeInTheDocument();
    expect(screen.getByText('安全')).toBeInTheDocument();
    expect(document.querySelector('script')).toBeNull();
  });

  it('does not keep javascript links', () => {
    render(<MarkdownViewer content={'[危险](javascript:alert(1))'} />);
    expect(screen.queryByRole('link', { name: '危险' })).toBeNull();
    expect(screen.getByText('危险')).toBeInTheDocument();
  });
});
