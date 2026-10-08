import { defineConfig } from 'vite';

// Client build goes to dist/client so devvit.json can serve it as the post webview.
export default defineConfig({
  base: './',
  build: { outDir: 'dist/client', emptyOutDir: true, chunkSizeWarningLimit: 900, target: 'es2020' },
});
