import { defineConfig } from "@playwright/test";
// The runner starts the synthetic fixture on every port the specs expect: 4109 for
// the dashboard and 4192 for tasks.spec.ts. A busy port fails the run instead of
// silently testing whatever already listens there; set BRAIN_TEST_UI_URL or
// BRAIN_TASK_UI_URL to use a server you started yourself.
const fixture = (port: number) => ({
  command: `python3 tests/fixture.py --port ${port}`,
  url: `http://127.0.0.1:${port}/api/state?k=demo`,
  reuseExistingServer: false,
  stdout: "ignore" as const,
  stderr: "pipe" as const,
  env: { FIXTURE_QUIET: "1" },
});
export default defineConfig({
  testDir: "tests",
  testMatch: "*.spec.ts",
  workers: 1,
  reporter: "list",
  webServer: [
    ...(process.env.BRAIN_TEST_UI_URL ? [] : [fixture(4109)]),
    ...(process.env.BRAIN_TASK_UI_URL ? [] : [fixture(4192)]),
  ],
  use: {
    baseURL: process.env.BRAIN_TEST_UI_URL || "http://127.0.0.1:4109",
    headless: true,
    screenshot: "off",
    trace: "off",
  },
  outputDir: "/tmp/brain-ui-playwright",
});
