import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const frontend = {
  host: '127.0.0.1',
  port: 5555,
  strictPort: true,
};
const backend = 'http://127.0.0.1:6666';
const proxy = {
  '/api': backend,
};

export default defineConfig({
  plugins: [react()],
  base: '/',
  server: {
    ...frontend,
    proxy,
  },
  preview: {
    ...frontend,
    proxy,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
