const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests/browser",
  workers: 1,
  timeout: 30000,
  use: {
    baseURL: "http://127.0.0.1:8766",
    viewport: { width: 1440, height: 1000 },
  },
  webServer: {
    command: "uv run python -m tests.smoke_server",
    url: "http://127.0.0.1:8766",
    reuseExistingServer: false,
    timeout: 20000,
  },
  outputDir: "artifacts/playwright",
});
