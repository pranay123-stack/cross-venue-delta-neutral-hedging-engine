import { defineConfig } from "@playwright/test";

/**
 * Browser tests drive the real dashboard against the real backend, replaying
 * the committed recording of a live Delta/Hyperliquid session -- offline and
 * reproducible.  Both servers are started here on ports that do not collide
 * with the Docker stack (5174/8001).
 */
const API_PORT = 8011;
const UI_PORT = 5181;
const backend = "../backend";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: `http://localhost:${UI_PORT}`,
    channel: "chrome",
    headless: true,
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command:
        `rm -f /tmp/hedgelab-e2e.db && ` +
        `HEDGELAB_DATABASE_URL=sqlite+aiosqlite:////tmp/hedgelab-e2e.db ` +
        `HEDGELAB_MARKET_DATA_SOURCE=replay ` +
        `HEDGELAB_MARKET_REPLAY_PATH=${backend}/tests/data/live_frames.jsonl.gz ` +
        `HEDGELAB_MARKET_POLL_SECONDS=1 HEDGELAB_ENVIRONMENT=test HEDGELAB_LOG_LEVEL=WARNING ` +
        `${backend}/.venv/bin/uvicorn hedgelab.api.app:app --port ${API_PORT}`,
      url: `http://localhost:${API_PORT}/api/health`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `VITE_API_TARGET=http://localhost:${API_PORT} npx vite --port ${UI_PORT} --strictPort`,
      url: `http://localhost:${UI_PORT}`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
