import { expect, test } from "@playwright/test";

/** The Phase 6 DoD smoke: boot → seeded chat with mock streaming → approve a
 *  batch. Runs keyless — chat is the demo mode (D-024), which drives the REAL
 *  router/runtime/tools against the seeded world, so this also exercises SSE,
 *  tracing, staging writes, and the review transition end to end. */

test.describe.configure({ mode: "serial" });

test("chat: counsel answer streams with routing badge and citations", async ({ page }) => {
  await page.goto("/chat");
  await expect(page.getByTestId("composer")).toBeEnabled();

  await page.getByTestId("composer").fill("What is Umbra's streaming royalty rate?");
  await page.getByTestId("send").click();

  // The SSE stream paints the route decision, then the final answer.
  await expect(page.getByTestId("route-badge").first()).toContainText("counsel");
  const answer = page.getByTestId("message-assistant").last();
  await expect(answer).toContainText("rate card", { timeout: 60_000 });
  await expect(answer).toContainText("§3");

  // Clause-chip citation opens the source drawer with the real clause text.
  await page.getByTestId("citation-chip").first().click();
  const drawer = page.getByTestId("clause-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText("ROYALTIES", { timeout: 20_000 });
  await page.keyboard.press("Escape");
  await expect(drawer).not.toBeVisible();
});

test("chat → reconcile → review queue → approve", async ({ page }) => {
  await page.goto("/chat");
  await page.getByTestId("composer").fill("Reconcile the 2026-04 statements");
  await page.getByTestId("send").click();

  await expect(page.getByTestId("route-badge").first()).toContainText("reconciler");
  // The reconciler workflow (scan + allocations through royaltycalc) takes a while.
  const batchLink = page.getByTestId("batch-link").last();
  await expect(batchLink).toBeVisible({ timeout: 110_000 });
  const batchText = await batchLink.innerText(); // "batch #N → review"
  const batchId = batchText.match(/#(\d+)/)?.[1];
  expect(batchId).toBeTruthy();

  // Review Queue: the proposed batch is listed with allocations and evidence.
  await batchLink.click();
  await page.waitForURL("**/review");
  await page.getByTestId(`batch-row-${batchId}`).click();
  const detail = page.getByTestId("batch-detail");
  await expect(detail).toContainText(`Batch #${batchId}`);
  await expect(page.getByTestId("promotion-panel")).toBeVisible();
  await expect(page.getByTestId("allocations-table").locator("tbody tr").first()).toBeVisible();

  // Reject requires a note: the confirm button stays disabled while it's empty.
  await page.getByTestId("reject").click();
  await expect(page.getByTestId("confirm-reject")).toBeDisabled();
  await page.keyboard.press("Escape");

  // Approve — the human half of invariant 5.
  await page.getByTestId("approve").click();
  await expect(page.locator("text=approved").first()).toBeVisible({ timeout: 20_000 });
});

test("trace inspector shows the span tree for the run", async ({ page }) => {
  await page.goto("/runs");
  // The reconciler run from the previous test is in the list; open the newest run.
  await page.getByTestId("run-list").locator("button").first().click();
  await expect(page.getByTestId("run-header")).toBeVisible();
  await expect(page.getByTestId("span-tree")).toBeVisible();
  await expect(page.getByTestId("span-llm_call").first()).toBeVisible();
  // A span click opens the detail drawer with its attrs.
  await page.getByTestId("span-llm_call").first().click();
  await expect(page.locator("text=gen_ai.usage.output_tokens").first()).toBeVisible();
});
