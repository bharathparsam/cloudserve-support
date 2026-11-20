import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";
const evidencePrefix =
  process.env.BROWSER_EVIDENCE_PREFIX || "evidence/real-degraded";
// Fixture-free browser integration. Local degraded mode is not hosted inference evidence.
test("real backend four channels, handoffs, source, upload, stop and refresh persistence", async ({
  page,
  request,
}) => {
  test.skip(
    !process.env.RUN_BACKEND_UI,
    "Set RUN_BACKEND_UI=1 and local OPERATOR_TOKEN to run against an actual backend",
  );
  test.setTimeout(180000);
  const token = process.env.OPERATOR_TOKEN;
  if (!token)
    throw Error("Local operator token required; do not put it in source");
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Overview", exact: true }),
  ).toBeVisible();
  await expect(
    page
      .getByText(/OpenRouter key missing|provider unavailable|degraded/i)
      .first(),
  ).toBeVisible();
  await page.getByRole("button", { name: "Connect operator" }).click();
  await page.getByLabel("Operator token", { exact: true }).fill(token);
  await page.getByRole("button", { name: "Use token" }).click();
  for (const name of [
    "Email",
    "Live chat",
    "Documentation",
    "Community forum",
  ]) {
    await page
      .getByRole("button", { name: "New ticket", exact: true })
      .first()
      .click();
    await page.getByRole("button", { name, exact: true }).click();
    await page.getByRole("button", { name: "Use synthetic example" }).click();
    await page
      .getByRole("button", { name: "Submit ticket", exact: true })
      .click();
    await expect(
      page.getByText("Contextual handoff", { exact: true }),
    ).toBeVisible({ timeout: 45000 });
    await expect(
      page.getByText("Human help required", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Committed simulated release")).toHaveCount(0);
  }
  const sourceButtons = page.locator(".source");
  if (await sourceButtons.count()) {
    await sourceButtons.first().click();
    await expect(
      page.getByRole("heading", { name: "Source passage" }),
    ).toBeVisible();
    await expect(page.locator(".source-text")).not.toBeEmpty();
    await page.getByRole("button", { name: "Close source" }).click();
  }
  await page.screenshot({
    path: `${evidencePrefix}-handoff-desktop.png`,
    fullPage: true,
  });
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Runs & evaluation" })
    .click();
  await page.locator("input[type=file]").setInputFiles({
    name: "synthetic-browser-input.json",
    mimeType: "application/json",
    buffer: Buffer.from(
      JSON.stringify([
        { channel: "email", body: "Please explain HTTP 429." },
        { channel: "chat", body: "Please issue a refund." },
        null,
      ]),
    ),
  });
  await page.getByRole("button", { name: "Launch development run" }).click();
  await expect(
    page.getByRole("heading", { name: "Run evidence" }),
  ).toBeVisible();
  await expect(
    page.getByText("Reconciliation: Complete", { exact: false }),
  ).toBeVisible({ timeout: 60000 });
  const runId = await page
    .locator(".runs-panel")
    .last()
    .locator("code")
    .first()
    .textContent();
  if (process.env.VERIFY_ARTIFACT_DOWNLOAD) {
    await expect
      .poll(
        async () => {
          const response = await request.get(
            `/api/v1/runs/${runId}/artifacts/manifest.json`,
            { headers: { Authorization: `Bearer ${token}` } },
          );
          if (!response.ok()) return "not_exported";
          return (await response.json()).state;
        },
        {
          timeout: 45000,
          message: "Wait for the persisted export manifest to complete",
        },
      )
      .toBe("completed");
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "metrics.json", exact: true }).click(),
    ]);
    const target = `${evidencePrefix}-downloaded-metrics.json`;
    await download.saveAs(target);
    const metrics = JSON.parse(await readFile(target, "utf8"));
    expect(metrics.counts.input).toBe(3);
    expect(metrics.counts.processed).toBe(3);
    expect(metrics.governance.reconciled).toBe(true);
  }
  await page.reload();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Runs & evaluation" })
    .click();
  await expect(page.getByText(runId!, { exact: true })).toBeVisible();
  // Reconnect after refresh: token deliberately has no persistence.
  await page.getByRole("button", { name: "Connect operator" }).click();
  await page.getByLabel("Operator token", { exact: true }).fill(token);
  await page.getByRole("button", { name: "Use token" }).click();
  const stop = await request.put("/api/v1/controls/automation", {
    headers: { Authorization: `Bearer ${token}` },
    data: { enabled: false, reason: "Real degraded browser verification stop" },
  });
  expect(stop.ok()).toBeTruthy();
  await page.getByRole("button", { name: "Refresh backend state" }).click();
  await expect(
    page.getByText("Automatic answers stopped", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Re-enable automation" }).click();
  await page
    .getByLabel("Operator reason")
    .fill("Verify missing-key enable is blocked");
  await page.getByRole("button", { name: "Verify and re-enable" }).click();
  await expect(page.getByText(/Free provider preflight failed/)).toBeVisible();
  await page.getByRole("button", { name: "Close dialog" }).click();
  const list = await (await request.get("/api/v1/tickets?limit=1")).json();
  const id = list.items[0].record_id;
  expect((await request.get(`/api/v1/tickets/${id}/original`)).status()).toBe(
    401,
  );
  expect(
    (
      await request.get(`/api/v1/tickets/${id}/original`, {
        headers: { Authorization: `Bearer ${token}` },
      })
    ).status(),
  ).toBe(200);
  for (const width of [375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    for (const name of [
      "Overview",
      "New ticket",
      "Ticket queue",
      "Runs & evaluation",
    ]) {
      await page
        .getByRole("navigation")
        .getByRole("button", { name, exact: false })
        .click();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth - innerWidth,
        ),
        `${name} ${width}`,
      ).toBe(0);
    }
    await page.screenshot({
      path: `${evidencePrefix}-${width}.png`,
      fullPage: true,
    });
  }
});

test("real MiniLM source passage opens from a persisted documented request", async ({
  page,
}) => {
  test.skip(!process.env.RUN_BACKEND_UI, "Actual backend opt-in only");
  test.setTimeout(90000);
  const token = process.env.OPERATOR_TOKEN;
  if (!token) throw Error("Local token required");
  await page.goto("/");
  await page.getByRole("button", { name: "Connect operator" }).click();
  await page.getByLabel("Operator token", { exact: true }).fill(token);
  await page.getByRole("button", { name: "Use token" }).click();
  await page
    .getByRole("button", { name: "New ticket", exact: true })
    .first()
    .click();
  await page
    .getByRole("textbox", { name: "Subject", exact: false })
    .fill("Documented API backoff question");
  await page
    .getByRole("textbox", { name: "Request body", exact: true })
    .fill("API HTTP 429 rate limits exponential backoff");
  await page
    .getByRole("button", { name: "Submit ticket", exact: true })
    .click();
  await expect(
    page.getByText("Contextual handoff", { exact: true }),
  ).toBeVisible({ timeout: 45000 });
  await expect(page.locator(".source").first()).toBeVisible();
  await page.locator(".source").first().click();
  await expect(
    page.getByRole("heading", { name: "Source passage" }),
  ).toBeVisible();
  await expect(page.locator(".source-text")).toContainText(
    /429|rate limit|backoff/i,
  );
  await expect(page.getByRole("dialog")).toContainText("DOC-API-001");
  await page.screenshot({
    path: `${evidencePrefix}-source-desktop.png`,
    fullPage: true,
  });
  await page.setViewportSize({ width: 375, height: 900 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - innerWidth,
    ),
  ).toBe(0);
  await page.screenshot({
    path: `${evidencePrefix}-source-mobile.png`,
    fullPage: true,
  });
});
