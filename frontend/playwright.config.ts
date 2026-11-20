import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  timeout: 30000,
  use: {
    baseURL: process.env.BASE_URL || "http://127.0.0.1:5173",
    headless: true,
  },
  webServer: process.env.BASE_URL
    ? undefined
    : {
        command: "npm run dev -- --port 5173",
        url: "http://127.0.0.1:5173",
        reuseExistingServer: true,
      },
  reporter: [
    ["list"],
    [
      "json",
      {
        outputFile:
          process.env.BROWSER_EVIDENCE_FILE ||
          (process.env.RUN_BACKEND_UI
            ? "evidence/backend-results.json"
            : "evidence/fixture-results.json"),
      },
    ],
  ],
});
