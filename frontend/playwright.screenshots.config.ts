import base from "./playwright.config";
import { defineConfig } from "@playwright/test";

/**
 * Captures README screenshots from the real running app (real API on the
 * recorded Delta/Hyperliquid session).  Not part of the test run:
 *   npx playwright test -c playwright.screenshots.config.ts
 */
export default defineConfig({
  ...base,
  testDir: "./screenshots",
  use: { ...base.use, viewport: { width: 1440, height: 900 } },
});
