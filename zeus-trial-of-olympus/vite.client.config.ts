import { defineConfig } from 'vite';

export default defineConfig({
  root: 'src/client',
  base: './',
  build: {
    outDir: '../../dist/client',
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
    target: 'es2020',
  },
});
