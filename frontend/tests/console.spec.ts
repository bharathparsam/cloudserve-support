import { test, expect } from "@playwright/test";
// Explicit UI contract fixtures: these tests are not live inference evidence.
const source = {
  doc_id: "DOC-API-001",
  chunk_id: "fixture-chunk",
  title: "Rate limit retry guidance",
  content: "Use exponential backoff after HTTP 429.",
  start: 0,
  end: 40,
  corpus_hash: "fixture-corpus",
  score: 0.88,
  score_kind: "cosine_similarity",
};
const dimension = {
  label: "rate_limit",
  raw_probability: 0.92,
  calibrated_probability: 0.86,
  raw_probabilities: { rate_limit: 0.92 },
  alternatives: [],
  provider_confidence: null,
};
const outcome = {
  record_id: "fixture-record",
  run_id: "fixture-run",
  input_index: 0,
  action: "auto_respond",
  blocked: false,
  reason: "All checks passed",
  answer:
    "Automated support response. Use exponential backoff. [DOC-API-001 / fixture-chunk]",
  classification: {
    status: "ok",
    intent: dimension,
    urgency: { ...dimension, label: "low" },
    calibration_status: "measured",
  },
  sources: [source],
  route: { reason_codes: ["supported"] },
  checks: [{ name: "grounding", status: "pass", reasons: [] }],
  provenance: "Fixture replay",
};
const ticket = {
  record_id: "fixture-record",
  ticket: {
    channel: "email",
    subject: "Rate limit guidance",
    body: "How do I retry HTTP 429?",
  },
  outcome,
  events: [
    {
      decision_id: "fixture-event",
      stage: "release",
      timestamp: "2026-11-19T00:00:00Z",
      data: { provenance: "Fixture replay" },
    },
  ],
  status: "completed",
};
async function fixtures(page: any) {
  await page.route("**/health/ready", (r) =>
    r.fulfill({ json: { ready: true, assets: true, worker: true } }),
  );
  await page.route("**/api/v1/**", async (route: any) => {
    const u = new URL(route.request().url()),
      p = u.pathname;
    let data: any = {};
    if (p.endsWith("/status"))
      data = {
        mode: "Fixture replay",
        status: "degraded",
        issues: ["Fixture replay: provider unavailable"],
        decider: {
          mode: "shadow",
          policy_ready: false,
          provider: {
            configured_model: "fixture/decider:free",
            verified: false,
          },
          fingerprint: "fixture-shadow-v1",
        },
      };
    else if (p.endsWith("/controls/automation"))
      data = { enabled: false, epoch: 0 };
    else if (p.endsWith("/tickets/fixture-record")) data = ticket;
    else if (p.endsWith("/sources/fixture-chunk")) data = source;
    else if (p.endsWith("/tickets") && route.request().method() === "POST")
      data = { record_id: "fixture-record", run_id: "fixture-run" };
    else if (p.endsWith("/tickets")) data = { items: [ticket], total: 1 };
    else if (p.endsWith("/runs")) data = { items: [], total: 0 };
    await route.fulfill({ json: data });
  });
}
test("console renders honest fixture provenance, committed answer and exact source", async ({
  page,
}) => {
  await fixtures(page);
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Overview", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Ticket queue", exact: false })
    .first()
    .click();
  await page
    .getByRole("button", { name: "Rate limit guidance", exact: true })
    .click();
  await expect(page.getByText("Fixture replay", { exact: true })).toBeVisible();
  await expect(page.getByText("Committed simulated release")).toBeVisible();
  await page.getByRole("button", { name: /Rate limit retry guidance/ }).click();
  await expect(page.getByText(source.content, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Close source" }).click();
  await page.getByRole("button", { name: "Re-enable automation" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).not.toBeVisible();
});
test("all channel inputs submit through authenticated API and token is not persisted", async ({
  page,
}) => {
  await fixtures(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Connect operator" }).click();
  await page
    .getByLabel("Operator token", { exact: true })
    .fill("fixture-token");
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
    await expect(page.getByText("Committed simulated release")).toBeVisible();
  }
  expect(await page.evaluate(() => Object.keys(localStorage))).toEqual([]);
  expect(await page.evaluate(() => Object.keys(sessionStorage))).toEqual([]);
});
for (const width of [375, 768, 1024, 1440])
  test(`no page overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await fixtures(page);
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Overview", exact: true }),
    ).toBeVisible();
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
        name,
      ).toBe(0);
    }
    await page.screenshot({
      path: `evidence/console-${width}.png`,
      fullPage: true,
    });
  });

test("protected original fetch requires in-memory token and clears the closed dialog", async ({
  page,
}) => {
  await fixtures(page);
  let calls = 0;
  await page.route(
    "**/api/v1/tickets/fixture-record/original",
    async (route) => {
      calls++;
      expect(route.request().headers()["authorization"]).toBe(
        "Bearer fixture-token",
      );
      await route.fulfill({
        json: {
          body: "Synthetic original <script>never execute</script>",
          customer_name: "Synthetic Customer",
        },
      });
    },
  );
  await page.goto("/");
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Ticket queue", exact: false })
    .click();
  await page
    .getByRole("button", { name: "Rate limit guidance", exact: true })
    .click();
  await page.getByRole("button", { name: "View protected original" }).click();
  await expect(
    page.getByText(
      "Connect a local operator before viewing protected original input.",
    ),
  ).toBeVisible();
  expect(calls).toBe(0);
  await page.getByRole("button", { name: "Connect operator" }).click();
  await page
    .getByLabel("Operator token", { exact: true })
    .fill("fixture-token");
  await page.getByRole("button", { name: "Use token" }).click();
  await page.getByRole("button", { name: "View protected original" }).click();
  await expect(
    page.getByRole("heading", { name: "Protected original request" }),
  ).toBeVisible();
  await expect(page.getByRole("dialog")).toContainText(
    "Synthetic original <script>never execute</script>",
  );
  expect(await page.locator(".source-text script").count()).toBe(0);
  expect(calls).toBe(1);
  await page.getByRole("button", { name: "Close original request" }).click();
  await expect(page.getByText(/Synthetic original <script>/)).toHaveCount(0);
});

test("Decider shadow readiness explicitly cannot authorize release", async ({
  page,
}) => {
  await fixtures(page);
  await page.goto("/");
  const gate = page
    .locator(".readiness-row")
    .filter({ hasText: "Decider safety gate" });
  await expect(gate).toContainText("shadow");
  await expect(
    page.getByText("Shadow evaluation — cannot authorize release", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.locator(".readiness-row").filter({ hasText: "Decider policy" }),
  ).toContainText("Not ready");
  await page
    .getByText("Artifact and provider details", { exact: true })
    .click();
  await expect(page.locator(".system-panel pre")).toContainText('"decider"');
  await expect(page.locator(".system-panel pre")).toContainText(
    "fixture-shadow-v1",
  );
  await expect(
    page.getByText("Committed simulated release", { exact: true }),
  ).toHaveCount(0);
});
