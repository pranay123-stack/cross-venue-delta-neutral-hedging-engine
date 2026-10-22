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
  await expect(page.getByText(/replay data/)).toBeVisible();
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
