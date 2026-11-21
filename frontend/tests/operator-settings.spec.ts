import { test, expect, type Page } from "@playwright/test";
// Explicit contract fixtures only. These tests never verify live model pricing or inference.
const initial = {
  generation_mode: "free_only",
  paid_budget: {limit_usd:"0.25",spent_usd:"0.0023492",reserved_usd:"0",remaining_usd:"0.2476508",blocked:false,reason:null},
  generation_model: "fixture/writer:free",
  decider_model: "fixture/decider:free",
  decider_mode: "shadow",
  timeout_seconds: 30,
  max_retries: 2,
  revision: 7,
  updated_at: null,
  credential_configured: true,
  policy_ready: false,
  source: "environment",
};
async function setup(
  page: Page,
  { enabled = false, saveFailure = false, discoveryFailure = false } = {},
) {
  const calls: {
    path: string;
    body: any;
    authorization: string | undefined;
  }[] = [];
  let settings = { ...initial };
  let runs: any[] = [];
  await page.route("**/health/ready", (route) =>
    route.fulfill({ json: { ready: true, assets: true, worker: true } }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname;
    let data: any = {};
    const method = request.method();
    if (
      [
        "/api/v1/settings",
        "/api/v1/models",
        "/api/v1/datasets",
        "/api/v1/datasets/launch",
        "/api/v1/datasets/template",
      ].includes(path)
    )
      expect(request.headers().authorization).toBe("Bearer fixture-operator");
    if (method !== "GET")
      calls.push({
        path,
        body: request.postDataJSON(),
        authorization: request.headers().authorization,
      });
    if (path === "/api/v1/status")
      data = {
        mode: "Fixture replay",
        generation_mode: settings.generation_mode,
        paid_budget: settings.paid_budget,
        provider: {configured_model:settings.generation_model},
        status: "degraded",
        issues: ["UI contract fixture only"],
        decider: { mode: "shadow", policy_ready: false },
        automation: { enabled, epoch: 1 },
      };
    else if (path === "/api/v1/controls/automation")
      data = { enabled, epoch: 1 };
    else if (path === "/api/v1/settings" && method === "GET") data = settings;
    else if (path === "/api/v1/settings" && method === "PUT") {
      if (saveFailure)
        return route.fulfill({
          status: 503,
          json: {
            error: {
              message:
                "Free model metadata verification failed; settings unchanged",
            },
          },
        });
      settings = {
        ...settings,
        ...request.postDataJSON(),
        revision: settings.revision + 1,
        source: "operator",
        updated_at: "2026-11-19T12:00:00Z",
      };
      data = settings;
    } else if (path === "/api/v1/models") {
      if (discoveryFailure)
        return route.fulfill({
          status: 503,
          json: {
            error: {
              message:
                "Catalog unavailable; manual free ID still requires verification",
            },
          },
        });
      data = {
        generation: [
          {
            id: "fixture/alternate-writer:free",
            name: "Fixture alternate writer",
          },
        ],
        decider: [
          {
            id: "fixture/alternate-decider:free",
            name: "Fixture alternate decider",
          },
        ],
        source: "fixture_catalog",
        note: "UI fixture metadata only; not live catalog evidence",
      };
    } else if (path === "/api/v1/datasets")
      data = {
        items: [
          {
            id: "development",
            name: "Supplied development tickets",
            record_count: 500,
            available: true,
            format: "cloudserve-ticket-array",
            description: "Frozen development input fixture",
          },
        ],
        template_url: "/api/v1/datasets/template",
      };
    else if (path === "/api/v1/datasets/launch") {
      const count = request.postDataJSON().limit ?? 500;
      runs = [
        {
          run_id: "fixture-bundled-run",
          state: "completed",
          expected_count: count,
          processed_count: count,
          kind: "development_builtin",
        },
      ];
      data = { run_id: "fixture-bundled-run", expected_count: count };
    } else if (path === "/api/v1/datasets/template")
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify([
          { channel: "email", body: "Synthetic template request" },
        ]),
      });
    else if (path === "/api/v1/runs")
      data = { items: runs, total: runs.length };
    else if (path === "/api/v1/runs/fixture-bundled-run") data = runs[0];
    else if (path === "/api/v1/runs/fixture-bundled-run/metrics")
      data = {
        counts: {
          input: runs[0].expected_count,
          processed: runs[0].expected_count,
        },
        governance: { reconciled: true, outcome_coverage: 1 },
        business: { auto_answer_rate: 0, confirmed_fcr: null },
        technical: { processing_p95_seconds: null },
      };
    else if (path === "/api/v1/tickets") data = { items: [], total: 0 };
    await route.fulfill({ json: data });
  });
  return calls;
}
async function connect(page: Page) {
  await page
    .getByRole("button", { name: "Connect operator", exact: true })
    .click();
  await page
    .getByLabel("Operator token", { exact: true })
    .fill("fixture-operator");
  await page.getByRole("button", { name: "Use token", exact: true }).click();
}
test("settings discover free choices, keep secrets private, enforce guard and persist exact save", async ({
  page,
}) => {
  const calls = await setup(page);
  await page.goto("/");
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Settings", exact: true })
    .click();
  await expect(
    page.getByText("Connect the local operator to load and edit settings."),
  ).toBeVisible();
  await connect(page);
  await expect(
    page.getByLabel("Generation model — free ID required"),
  ).toHaveValue(initial.generation_model);
  await expect(
    page.getByText("Provider key configured", { exact: true }).locator(".."),
  ).toContainText("Configured");
  await expect(
    page.locator("select[name=decider_mode]").locator("option[value=enforce]"),
  ).toHaveAttribute("disabled", "");
  await expect(
    page.getByText(/Enforce unavailable: no calibrated/),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Discover free models", exact: true })
    .click();
  await expect(page.getByText(/fixture catalog/)).toBeVisible();
  await page
    .getByLabel("Generation model — free ID required")
    .fill("fixture/alternate-writer:free");
  await page.getByLabel("Model timeout (seconds)").fill("45");
  await page.getByLabel("Maximum retries").fill("1");
  await page.locator("select[name=decider_mode]").selectOption("disabled");
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .click();
  await expect(
    page.getByText(
      "Settings saved durably at revision 8. Automatic answers remain stopped.",
    ),
  ).toBeVisible();
  const save = calls.find((c) => c.path === "/api/v1/settings");
  expect(save?.body).toEqual({
    generation_mode: "free_only",
    generation_model: "fixture/alternate-writer:free",
    decider_model: initial.decider_model,
    decider_mode: "disabled",
    timeout_seconds: 45,
    max_retries: 1,
  });
  expect(save?.authorization).toBe("Bearer fixture-operator");
  expect(JSON.stringify(save?.body)).not.toContain("fixture-operator");
  await expect(page.locator("input[type=password]")).toHaveCount(0);
  await page.setViewportSize({ width: 375, height: 900 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - innerWidth,
    ),
  ).toBe(0);
  await page.screenshot({
    path: "evidence/operator-settings-mobile.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({
    path: "evidence/operator-settings-desktop.png",
    fullPage: true,
  });
});
test("save and discovery failures preserve stored revision and allow manual free ID", async ({
  page,
}) => {
  await setup(page, { saveFailure: true, discoveryFailure: true });
  await page.goto("/");
  await connect(page);
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Settings", exact: true })
    .click();
  await expect(
    page.getByLabel("Generation model — free ID required"),
  ).toHaveValue(initial.generation_model);
  await page
    .getByRole("button", { name: "Discover free models", exact: true })
    .click();
  await expect(
    page.getByText(
      "Catalog unavailable; manual free ID still requires verification",
    ),
  ).toBeVisible();
  await page
    .getByLabel("Generation model — free ID required")
    .fill("fixture/manual-writer:free");
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .click();
  await expect(
    page.getByText(
      "Free model metadata verification failed; settings unchanged",
    ),
  ).toBeVisible();
  await expect(
    page.getByText("Settings revision", { exact: true }).locator(".."),
  ).toContainText("7");
  await expect(page.getByText(/Settings saved durably/)).toHaveCount(0);
});
test("bundled first N and all 500 launch exact durable runs; template uses authenticated download", async ({
  page,
}) => {
  const calls = await setup(page, { enabled: true });
  await page.goto("/");
  await connect(page);
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Runs & evaluation", exact: true })
    .click();
  await expect(
    page.getByText("Supplied development tickets", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Development record selection").selectOption("first");
  await page.getByLabel("Number of first records").fill("3");
  await page
    .getByRole("button", {
      name: "Launch bundled development run",
      exact: true,
    })
    .click();
  await expect(
    page.getByText("Reconciliation: Complete", { exact: false }),
  ).toBeVisible();
  expect(calls.find((c) => c.path === "/api/v1/datasets/launch")?.body).toEqual(
    { dataset_id: "development", limit: 3 },
  );
  await expect(
    page.getByText("fixture-bundled-run", { exact: true }).first(),
  ).toBeVisible();
  await page.getByLabel("Development record selection").selectOption("all");
  await page
    .getByRole("button", {
      name: "Launch bundled development run",
      exact: true,
    })
    .click();
  await expect(
    page.getByText("Bundled development run queued with 500 input positions."),
  ).toBeVisible();
  expect(
    calls.filter((c) => c.path === "/api/v1/datasets/launch").at(-1)?.body,
  ).toEqual({ dataset_id: "development", limit: null });
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page
      .getByRole("button", {
        name: "Download sample JSON template",
        exact: true,
      })
      .click(),
  ]);
  expect(download.suggestedFilename()).toBe("cloudserve-ticket-template.json");
  await page.getByText("Exact accepted JSON format", { exact: true }).click();
  await expect(
    page.getByText(/Upload a UTF-8 JSON array of ticket objects/),
  ).toBeVisible();
  await page.setViewportSize({ width: 375, height: 900 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - innerWidth,
    ),
  ).toBe(0);
  await page.screenshot({
    path: "evidence/operator-datasets-mobile.png",
    fullPage: true,
  });
});
test("bundled launch remains blocked while automation is stopped", async ({
  page,
}) => {
  const calls = await setup(page);
  await page.goto("/");
  await connect(page);
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Runs & evaluation", exact: true })
    .click();
  await expect(
    page.getByText("Supplied development tickets", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", {
      name: "Launch bundled development run",
      exact: true,
    })
    .click();
  await expect(
    page.getByText(/Enable automatic answers explicitly before launching/),
  ).toBeVisible();
  expect(calls.some((c) => c.path === "/api/v1/datasets/launch")).toBe(false);
});

test("paid development mode requires explicit consent and saves the selected policy without enabling automation",async({page})=>{
 const calls=await setup(page);await page.goto('/');await connect(page);
 await page.getByRole('navigation').getByRole('button',{name:'Settings',exact:true}).click();
 await page.locator('#generation-mode').selectOption('paid_test');
 await expect(page.locator('#generation-model')).toHaveValue('openai/gpt-4.1-mini');
 await expect(page.getByLabel('Maximum retries',{exact:true})).toHaveValue('0');
 await expect(page.getByLabel('Maximum retries',{exact:true})).toBeDisabled();
 await page.getByRole('button',{name:'Save settings',exact:true}).click();
 expect(calls.some(c=>c.path==='/api/v1/settings')).toBe(false);
 await page.getByLabel('I acknowledge paid development testing within the shared USD0.25 cap.',{exact:true}).check();
 await page.getByRole('button',{name:'Save settings',exact:true}).click();
 await expect(page.getByText('Settings saved durably at revision 8. Automatic answers remain stopped.')).toBeVisible();
 const save=calls.find(c=>c.path==='/api/v1/settings');
 expect(save?.body).toEqual({generation_mode:'paid_test',generation_model:'openai/gpt-4.1-mini',decider_model:initial.decider_model,decider_mode:'shadow',timeout_seconds:30,max_retries:0});
 expect(calls.some(c=>c.path==='/api/v1/controls/automation')).toBe(false);
 await expect(page.getByText('Spent: USD 0.0023492',{exact:true})).toBeVisible();
 await expect(page.getByText('Remaining: USD 0.2476508',{exact:true})).toBeVisible();
 await expect(page.getByRole('button',{name:/reset budget|top.up/i})).toHaveCount(0);
 await expect(page.getByText('Paid development test mode',{exact:true})).toBeVisible();
 await page.setViewportSize({width:375,height:1000});
 expect(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth)).toBe(0);
 await page.screenshot({path:'evidence/paid-ui-settings-mobile.png',fullPage:true});
});
