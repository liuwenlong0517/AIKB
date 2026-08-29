import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Vite 同时承担开发服务器和 Vitest 配置；代理只负责把浏览器请求转发给本地后端，
// 不在前端引入文件系统、数据库或脚本执行能力。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './tests/setup.ts',
    globals: true,
  },
});
