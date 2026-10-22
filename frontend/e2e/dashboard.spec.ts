import { expect, test, type Page } from "@playwright/test";

const API = "http://localhost:8011/api";
const PAGES: Array<[string, string]> = [
  ["/", "Overview"],
  ["/pairs", "Hedge Pairs"],
  ["/calculator", "Hedge Calculator"],
  ["/risk", "Risk Dashboard"],
  ["/orders", "Orders"],
  ["/cycles", "Hedge Cycles"],
  ["/configuration", "Configuration"],
  ["/faults", "Fault Injection"],
];

function collectErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));
  page.on("console", (msg) => {
    // Failed requests are reported below with their URL instead.
    if (msg.type() === "error" && !msg.text().startsWith("Failed to load resource")) {
      errors.push(msg.text());
    }
  });
  page.on("response", (response) => {
    if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`);
  });
  return errors;
}

test.beforeEach(async ({ request }) => {
  // Start every test from a clean paper book: no faults, no kill switch.
  await request.delete(`${API}/fault-injection`);
  await request.delete(`${API}/emergency/kill-switch`);
});

test("every page renders without a console error", async ({ page }) => {
  const errors = collectErrors(page);
  for (const [path, title] of PAGES) {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: title, level: 2 })).toBeVisible();
  }
  expect(errors).toEqual([]);
});

test("the top bar says paper only, real replayed data, both venues", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("PAPER TRADING ONLY")).toBeVisible();
  await expect(page.locator(".topbar").getByText("REPLAY DATA")).toBeVisible();
  await expect(page.locator(".topbar").getByText("PAPER_DELTA")).toBeVisible();
  await expect(page.locator(".topbar").getByText("PAPER_HYPERLIQUID")).toBeVisible();
});

test("hedge pairs show the four Delta to Hyperliquid pairs", async ({ page }) => {
  await page.goto("/pairs");
  for (const coin of ["BTC", "ETH", "SOL", "PAXG"]) {
    await expect(page.getByText(`${coin} Delta perp -> Hyperliquid perp`).first()).toBeVisible();
  }
});

test("the calculator sizes 5000 Delta contracts as 5 BTC on Hyperliquid", async ({ page }) => {
  await page.goto("/calculator");
  await page.getByRole("button", { name: "Calculate" }).click();
  await expect(page.getByText(/rounded to -5\b/)).toBeVisible();
  // The derivation is shown step by step, formula and value.
  await expect(page.getByText("Venue rounding")).toBeVisible();
  await expect(page.getByText("Margin", { exact: true })).toBeVisible();
});

test("a hedge executed from the dashboard completes and appears as a cycle", async ({ page, request }) => {
  const opened = await request.post(`${API}/hedge/open-position`, {
    data: { instrument_key: "PAPER_DELTA:BTCUSD", quantity: "2000" },
  });
  expect(opened.ok()).toBeTruthy();
  await page.goto("/pairs");
  const pair = page.locator(".panel", { hasText: "BTC Delta perp -> Hyperliquid perp" }).first();
  await pair.getByRole("button", { name: "Hedge existing position" }).click();
  await expect(page.getByText(/cycle ended COMPLETED/)).toBeVisible();
  await page.goto("/cycles");
  await expect(page.getByText("COMPLETED").first()).toBeVisible();
});

test("injecting a Hyperliquid outage shows it disconnected, clearing restores it", async ({ page }) => {
  await page.goto("/faults");
  await page.getByRole("button", { name: "Simulate Hyperliquid disconnect" }).click();
  const row = page.locator("dt", { hasText: "PAPER_HYPERLIQUID" }).locator("xpath=following-sibling::dd[1]");
  await expect(row).toHaveText("DISCONNECTED");
  await page.getByRole("button", { name: "Clear all faults and reconnect" }).click();
  await expect(row).toHaveText("connected");
});

test("the first screen says where prices come from and what execution is", async ({ page }) => {
  await page.goto("/");
  const summary = page.getByRole("region", { name: "System at a glance" });
  await expect(summary.getByText("PAPER", { exact: true })).toBeVisible();
  await expect(summary.getByText("not implemented")).toBeVisible();
  await expect(summary.getByText("RECORDED REPLAY")).toBeVisible();
  await expect(summary.getByText(/frame \d+ of 500/)).toBeVisible();
  await expect(summary.getByText("Every price shown is the real market, unmodified.")).toBeVisible();
  await expect(summary.getByText("2 / 2 connected")).toBeVisible();
});

test("a stress overlay is labelled as hypothetical until it is cleared", async ({ page, request }) => {
  await request.post(`${API}/market/stress`, { data: { scenario: "SPREAD_WIDENING" } });
  await page.goto("/");
  const summary = page.getByRole("region", { name: "System at a glance" });
  await expect(summary.getByText("HYPOTHETICAL PRICES")).toBeVisible();
  await expect(summary.getByText(/SPREAD_WIDENING/)).toBeVisible();
  await request.delete(`${API}/market/stress`);
  await page.reload();
  await expect(summary.getByText("NONE", { exact: true })).toBeVisible();
});

test("funding is shown per interval and annualised so the venues compare", async ({ page }) => {
  await page.goto("/");
  const market = page.locator(".panel", { hasText: "Market data" }).last();
  await expect(market.getByText(/bps \/ 8h/).first()).toBeVisible();     // Delta, 8-hourly
  await expect(market.getByText(/bps \/ 1h/).first()).toBeVisible();     // Hyperliquid, hourly
  await expect(market.getByText("Annualised", { exact: true })).toBeVisible();
  await expect(market.getByText(/bps \/ 4h/).first()).toBeVisible();     // Delta PAXG, 4-hourly
});

test("an unreachable API is reported, not left loading forever", async ({ page }) => {
  await page.route("**/api/accounts", (route) => route.abort());
  await page.route("**/api/portfolio", (route) => route.abort());
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("cannot reach the API");
});
