import { defineConfig } from 'vite';

export default defineConfig({
  ssr: { noExternal: true },
  build: {
    ssr: 'src/server/index.ts',
    outDir: 'dist/server',
    emptyOutDir: true,
    target: 'node22',
    rollupOptions: { output: { format: 'cjs', entryFileNames: 'index.cjs', inlineDynamicImports: true } },
  },
});
