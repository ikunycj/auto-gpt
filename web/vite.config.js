import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const host = '127.0.0.1';
const frontendPort = 5555;
const backend = 'http://127.0.0.1:6666';
const proxy = {
  '/api': backend,
  '/login': backend,
  '/logout': backend,
};

export default defineConfig({
  plugins: [react()],
  base: '/',
  server: {
    host,
    port: frontendPort,
    strictPort: true,
    proxy,
  },
  preview: {
    host,
    port: frontendPort,
    strictPort: true,
    proxy,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
