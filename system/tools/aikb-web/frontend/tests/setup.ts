import '@testing-library/jest-dom/vitest';

// jsdom 没有浏览器响应式 API；Ant Design 的 Row/Descriptions 在真实浏览器中依赖该 API。
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({ matches: false, media: query, onchange: null, addListener: () => undefined, removeListener: () => undefined, addEventListener: () => undefined, removeEventListener: () => undefined, dispatchEvent: () => false }),
});
