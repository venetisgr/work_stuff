import { resolve } from 'node:path';
import { defineConfig } from 'vite';

export default defineConfig({
  root: 'src/client',
  base: './',
  build: {
    outDir: '../../dist/client',
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
    target: 'es2020',
    rollupOptions: {
      // splash.html is the inline post card; game.html is the full game opened in expanded mode.
      input: {
        splash: resolve(__dirname, 'src/client/splash.html'),
        game: resolve(__dirname, 'src/client/game.html'),
      },
    },
  },
});
