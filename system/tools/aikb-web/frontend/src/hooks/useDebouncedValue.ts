import { useEffect, useState } from 'react';

/**
 * 将高频输入延迟为稳定值；仅用于筛选请求，选择器等离散操作仍可立即生效。
 * value 变化后会在 delay 毫秒内取消旧计时，卸载时不会再写入状态。
 */
export function useDebouncedValue<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [delay, value]);
  return debounced;
}
