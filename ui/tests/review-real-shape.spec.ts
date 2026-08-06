import { expect, test, type Page } from "@playwright/test";

/** Synthetic-vs-real drift guard (Phase 6 verification, finding 1).
 *
 *  The smoke approves a demo-mode batch whose agent-authored JSONB (allocation
 *  line_detail, flag payloads) is string-shaped, because the demo script writes
 *  `str(Decimal)`. Live-agent batches (run c804b338's multi_step reconcilers) write
 *  JSON numbers, omit or null keys, use numeric-string ids and line_ids lists, and
 *  put objects in `detail` — which white-screened the queue with
 *  "TypeError: e.trim is not a function". This spec serves a live-shaped
 *  BatchDetail via route interception (no backend state needed) and pins that the
 *  Review Queue renders every one of those shapes without a page error. */

const BATCH = {
  id: 9001,
  period: "2026-07",
  status: "proposed",
  submitted_by_run: null, // nullable: not every batch links a run
  summary: { n_allocations: 3, flags_by_severity: { error: 1, warning: 1, info: 1 } }, // no note
  created_at: "2026-08-06T09:00:00Z",
  n_allocations: 3,
  n_flags: 3,
  total_net_payable: "13252.170000",
};

const DETAIL = {
  batch: BATCH,
  allocations: [
    {
      // live-agent shape: JSON numbers, negative balance
      artist_id: 41,
      stage_name: "Beatriz Romano",
      period: "2026-07",
      net_payable: "12841.520000",
      line_detail: { gross: 21204.5678, recouped: 8363.05, balance_after: -12.5 },
    },
    {
      // nothing in line_detail at all, and no stage name resolved
      artist_id: 77,
      stage_name: null,
      period: "2026-07",
      net_payable: "401.150000",
      line_detail: {},
    },
    {
      // demo-style strings mixed with an explicit null
      artist_id: 78,
      stage_name: "Umbra",
      period: "2026-07",
      net_payable: "9.500000",
      line_detail: { gross: "19.000000", recouped: null },
    },
  ],
  flags: [
    {
      id: 1,
      kind: "duplicate_line",
      severity: "error",
      // no line_id, no detail — a line_ids list plus free-form measurements
      payload: { source: "staged", line_ids: [88101, 88102], line_hash: "c8f3a1", observed: 2 },
      evidence: [
        {
          source: "staged",
          id: 88101,
          statement_id: 3,
          period: "2026-07",
          isrc: "USFBR2600120",
          upc: null,
          store: "spotify",
          territory: "US",
          units: 1804,
          gross_amount: "7.216000",
          currency: "USD",
        },
        {
          source: "staged",
          id: 88102,
          statement_id: 3,
          period: "2026-07",
          isrc: "",
          upc: "198000000120",
          store: "bandcamp",
          territory: "DE",
          units: -3,
          gross_amount: "-42.000000",
          currency: "EUR",
        },
      ],
    },
    {
      id: 2,
      kind: "sudden_territory_spike",
      severity: "warning",
      // numeric-string line_id, object-shaped detail
      payload: {
        line_id: "88110",
        detail: { territory: "BR", observed_gross: 913.4, baseline: 120.7 },
      },
      evidence: [],
    },
    {
      id: 3,
      kind: "coverage",
      severity: "info",
      payload: {}, // nothing at all
      evidence: [],
    },
  ],
  promotion: {
    statements_to_promote: [],
    n_staged_lines: 0,
    staged_gross_by_currency: {},
    allocation_total: "13252.170000",
    n_paid_artists: 3,
  },
};

async function serveFixture(page: Page): Promise<string[]> {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  await page.route(/\/review\/batches(\?.*)?$/, (route) =>
    route.fulfill({ json: [BATCH] }),
  );
  await page.route(/\/review\/batches\/9001$/, (route) => route.fulfill({ json: DETAIL }));
  return pageErrors;
}

test("review queue renders a live-agent-shaped batch without crashing", async ({ page }) => {
  const pageErrors = await serveFixture(page);
  await page.goto("/review");

  // The list row formats the batch total; selecting it auto-loads the detail pane —
  // exactly the path that crashed on real staging data.
  const row = page.getByTestId("batch-row-9001");
  await expect(row).toBeVisible();
  await expect(row).toContainText("13,252.17");

  const detail = page.getByTestId("batch-detail");
  await expect(detail).toContainText("Batch #9001");

  // Allocations: numbers format, empty/null line_detail shows "—", strings still work.
  const rows = page.getByTestId("allocations-table").locator("tbody tr");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toContainText("21,204.56");
  await expect(rows.nth(0)).toContainText("−12.50");
  await expect(rows.nth(1)).toContainText("artist 77"); // null stage_name fallback
  await expect(rows.nth(1)).toContainText("—");
  await expect(rows.nth(2)).toContainText("19.00");

  // Flags: line_ids list ref, agent measurements, object detail, empty payload.
  const flags = page.getByTestId("flag-card");
  await expect(flags).toHaveCount(3);
  await expect(flags.nth(0)).toContainText("staged:88101 +1");
  await expect(flags.nth(0)).toContainText("line_hash=c8f3a1");
  await expect(flags.nth(0)).toContainText("−42.00 EUR"); // negative evidence line
  await expect(flags.nth(1)).toContainText("label:88110");
  await expect(flags.nth(1)).toContainText("observed_gross");
  await expect(flags.nth(2)).toContainText("coverage");

  // A proposed batch stays actionable.
  await expect(page.getByTestId("approve")).toBeVisible();
  await expect(page.getByTestId("reject")).toBeVisible();

  expect(pageErrors).toEqual([]);
});
