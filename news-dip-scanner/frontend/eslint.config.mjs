import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    rules: {
      // Every path but / and /ideas/<id> is the Fly app's, served through the catch-all route handler, so the rule
      // takes /settings, /news, /logout... for pages of this app. Links to them must be plain <a> (a full page load
      // through the proxy); links between this app's own pages use <Link>.
      "@next/next/no-html-link-for-pages": "off",
    },
  },
  globalIgnores([".next/**", "out/**", "build/**", "next-env.d.ts"]),
]);

export default eslintConfig;
