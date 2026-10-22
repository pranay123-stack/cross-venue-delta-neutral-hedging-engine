import { test } from "@playwright/test";

const API = "http://localhost:8011/api";
const OUT = "../docs/images";

test("capture README screenshots", async ({ page, request }) => {
  // Real state, produced through the API: a hedged BTC position.
  await request.delete(`${API}/fault-injection`);
  await request.post(`${API}/hedge/open-position`, {
    data: { instrument_key: "PAPER_DELTA:BTCUSD", quantity: "5000" },
  });
  await request.post(`${API}/hedge/execute`, {
    data: { mapping_name: "BTC Delta perp -> Hyperliquid perp", source_quantity: "0" },
  });

  const shots: Array<[string, string]> = [
    ["/", "overview"],
    ["/pairs", "hedge-pairs"],
    ["/risk", "risk-dashboard"],
    ["/cycles", "hedge-cycles"],
    ["/faults", "fault-injection"],
  ];
  for (const [path, name] of shots) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false });
  }

  await page.goto("/calculator");
  await page.getByRole("button", { name: "Calculate" }).click();
  await page.getByText("Venue rounding").waitFor();
  await page.screenshot({ path: `${OUT}/hedge-calculator.png`, fullPage: true });
});
