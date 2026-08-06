import { defineConfig } from "@playwright/test";

/** Playwright smoke (BUILD_PLAN Phase 6 DoD): boot → seeded chat with mock
 *  streaming → approve a batch. Expects the API on :8000 (keyless demo mode,
 *  seeded world) and the UI on :3000 — CI boots both in the workflow. */
export default defineConfig({
  testDir: "./tests",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  retries: process.env.CI ? 1 : 0,
  workers: 1, // the flow is stateful (chat → review); keep it serial
  reporter: process.env.CI ? [["list"], ["github"]] : [["list"]],
  use: {
    baseURL: process.env.UI_URL ?? "http://localhost:3000",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    // Environments with a pre-provisioned Chromium (sandboxes, containers) point
    // PW_CHROMIUM at its binary instead of downloading a matching build; CI and
    // dev machines leave it unset and use `playwright install chromium`.
    ...(process.env.PW_CHROMIUM
      ? { launchOptions: { executablePath: process.env.PW_CHROMIUM } }
      : {}),
  },
});
