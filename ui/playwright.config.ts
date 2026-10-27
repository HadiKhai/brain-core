import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "tests",
  testMatch: "*.spec.ts",
  workers: 1,
  reporter: "list",
  use: {
    baseURL: process.env.BRAIN_TEST_UI_URL || "http://127.0.0.1:4109",
    headless: true,
    screenshot: "off",
    trace: "off",
  },
  outputDir: "/tmp/brain-ui-playwright",
});
