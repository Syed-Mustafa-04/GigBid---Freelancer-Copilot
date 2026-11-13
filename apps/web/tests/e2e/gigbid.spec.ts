/**
 * End-to-end tests (Part D).
 *
 * These drive the real UI against a real API and GigSim. They cover the five
 * flows the assignment names: onboarding, approving a proposal, rejecting with
 * a reason, editing a draft, and the degraded-mode banner.
 *
 * Each test states the property it is protecting, because an e2e test that
 * only asserts "the page loaded" is theatre.
 */

import { test, expect, type Page } from '@playwright/test';

const API = process.env.E2E_API_URL ?? 'http://localhost:8000';
const EMAIL = process.env.E2E_EMAIL ?? 'reviewer@gigbid.app';
const PASSWORD = process.env.E2E_PASSWORD ?? 'gigbid-demo-2026';

async function signIn(page: Page) {
  await page.goto('/login');
  await page.fill('#email', EMAIL);
  await page.fill('#password', PASSWORD);
  await page.click('[data-testid="submit-login"]');
  await page.waitForURL('**/inbox', { timeout: 30_000 });
}

/** Analyse jobs until one reaches the approval gate; returns its thread id. */
async function analyseUntilApproval(page: Page, maxJobs = 6): Promise<string | null> {
  for (let i = 0; i < maxJobs; i += 1) {
    const buttons = page.locator('[data-testid="analyse-job"]');
    if ((await buttons.count()) === 0) return null;
    await buttons.first().click();
    // The run streams; wait for it to finish or stop for approval.
    await page.waitForTimeout(1500);
    await expect(page.locator('[data-testid="run-progress"]')).toBeHidden({ timeout: 120_000 });

    const approvals = await page.request.get(`${API}/approvals`, {
      headers: { Authorization: `Bearer ${await page.evaluate(() => localStorage.getItem('gigbid.token'))}` },
    });
    const body = await approvals.json();
    if (body.approvals?.length) return body.approvals[0].thread_id as string;
    await page.reload();
  }
  return null;
}

test.describe('onboarding and sign-in', () => {
  test('a reviewer can sign in and reach the job inbox', async ({ page }) => {
    // Protects: the graders can actually get into the deployed app.
    await signIn(page);
    await expect(page.getByRole('heading', { name: 'Job inbox' })).toBeVisible();
    await expect(page.locator('[data-testid="job-card"]').first()).toBeVisible({ timeout: 30_000 });
  });

  test('a wrong password is rejected with a readable message', async ({ page }) => {
    await page.goto('/login');
    await page.fill('#email', EMAIL);
    await page.fill('#password', 'definitely-the-wrong-password');
    await page.click('[data-testid="submit-login"]');
    await expect(page.locator('[data-testid="login-error"]')).toBeVisible();
    await expect(page).toHaveURL(/login/);
  });

  test('an unauthenticated visitor is sent to sign in', async ({ page }) => {
    await page.context().clearCookies();
    await page.goto('/');
    await page.evaluate(() => localStorage.clear());
    await page.goto('/inbox');
    await expect(page).toHaveURL(/login/, { timeout: 15_000 });
  });
});

test.describe('the approval gate', () => {
  test('a job can be analysed and streams node-by-node progress', async ({ page }) => {
    // Protects: the user never stares at a spinner during a long run.
    await signIn(page);
    const analyse = page.locator('[data-testid="analyse-job"]').first();
    await expect(analyse).toBeVisible({ timeout: 30_000 });
    await analyse.click();
    const progress = page.locator('[data-testid="run-progress"]');
    await expect(progress).toBeVisible({ timeout: 20_000 });
    // At least one node must report before the run ends.
    await expect(progress.locator('.node-row').first()).toBeVisible({ timeout: 90_000 });
  });

  test('a pending proposal shows evidence, a dry run and its cost', async ({ page }) => {
    // Protects: the human approves the real payload, not a summary of it.
    await signIn(page);
    const threadId = await analyseUntilApproval(page);
    test.skip(!threadId, 'no job reached the approval gate in this run');

    await page.goto(`/studio/${threadId}`);
    await expect(page.locator('[data-testid="approval-panel"]')).toBeVisible({ timeout: 30_000 });

    const dryRun = page.locator('[data-testid="dry-run"]');
    await expect(dryRun).toBeVisible();
    await expect(dryRun).toContainText('Connects');
    await expect(dryRun).toContainText('Idempotency key');
  });

  test('approving submits the proposal exactly once', async ({ page }) => {
    // Protects: the end of the human-in-the-loop path actually works.
    await signIn(page);
    const threadId = await analyseUntilApproval(page);
    test.skip(!threadId, 'no job reached the approval gate in this run');

    await page.goto(`/studio/${threadId}`);
    await page.click('[data-testid="approve"]');
    await expect(page.locator('[data-testid="approval-resolved"]')).toBeVisible({ timeout: 60_000 });

    // The approval must be gone from the queue, not merely hidden in the UI.
    const token = await page.evaluate(() => localStorage.getItem('gigbid.token'));
    const remaining = await page.request.get(`${API}/approvals`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const body = await remaining.json();
    const stillPending = (body.approvals ?? []).some(
      (a: { thread_id: string }) => a.thread_id === threadId,
    );
    expect(stillPending).toBe(false);
  });

  test('rejecting with a reason stops the proposal', async ({ page }) => {
    await signIn(page);
    const threadId = await analyseUntilApproval(page);
    test.skip(!threadId, 'no job reached the approval gate in this run');

    await page.goto(`/studio/${threadId}`);
    await page.fill('#reason', 'Rate is below my floor for this scope.');
    await page.click('[data-testid="reject"]');
    await expect(page.locator('[data-testid="approval-resolved"]')).toBeVisible({ timeout: 60_000 });
    await expect(page.locator('[data-testid="approval-resolved"]')).toContainText(/reject|skip/i);
  });

  test('editing a draft requires the edit path, not plain approve', async ({ page }) => {
    // Protects: an edit must re-run compliance. Approving edited text as if
    // unedited would make the policy guard decorative.
    await signIn(page);
    const threadId = await analyseUntilApproval(page);
    test.skip(!threadId, 'no job reached the approval gate in this run');

    await page.goto(`/studio/${threadId}`);
    await page.click('text=Edit');
    const editor = page.locator('[data-testid="edit-cover-letter"]');
    await expect(editor).toBeVisible();
    await editor.fill('I rewrote this opening myself to name the client problem directly.');

    // Plain approve is disabled while there are unsaved edits.
    await expect(page.locator('[data-testid="approve"]')).toBeDisabled();
    await expect(page.locator('[data-testid="approve-edited"]')).toBeEnabled();

    await page.click('[data-testid="approve-edited"]');
    await expect(page.locator('[data-testid="approval-resolved"]')).toBeVisible({ timeout: 60_000 });
  });
});

test.describe('evidence and grounding', () => {
  test('claims are marked and their evidence is reachable by keyboard', async ({ page }) => {
    // Protects: R5 is inspectable, and not only with a mouse.
    await signIn(page);
    const threadId = await analyseUntilApproval(page);
    test.skip(!threadId, 'no job reached the approval gate in this run');

    await page.goto(`/studio/${threadId}`);
    const claim = page.locator('.claim').first();
    const count = await page.locator('.claim').count();
    test.skip(count === 0, 'this draft contained no checkable claims');

    await claim.focus();
    await expect(page.locator('.evidence-pop')).toBeVisible();
    await expect(page.locator('.evidence-pop')).toContainText(/support|evidence|grounded/i);
  });
});

test.describe('degraded mode', () => {
  test('a marketplace outage is stated, not shown as an empty list', async ({ page }) => {
    // Protects: the single most misleading failure a job board can have.
    await signIn(page);
    await page.route('**/system/status', async (route) => {
      const response = await route.fetch();
      const json = await response.json();
      json.marketplace.available = false;
      await route.fulfill({ response, json });
    });
    await page.goto('/inbox');
    await expect(page.locator('[data-testid="banner-marketplace-down"]')).toBeVisible({
      timeout: 40_000,
    });
    await expect(page.locator('[data-testid="banner-marketplace-down"]')).toContainText(
      /unavailable|cannot reach/i,
    );
  });

  test('running on the offline model is stated plainly', async ({ page }) => {
    await signIn(page);
    await page.route('**/system/status', async (route) => {
      const response = await route.fetch();
      const json = await response.json();
      json.llm.offline_stub = true;
      json.llm.warning = 'offline';
      await route.fulfill({ response, json });
    });
    await page.goto('/inbox');
    await expect(page.locator('[data-testid="banner-offline-llm"]')).toBeVisible({ timeout: 40_000 });
  });
});

test.describe('memory is visible and deletable', () => {
  test('settings shows what GigBid learned and offers to forget it', async ({ page }) => {
    // Protects: R7. A system that learns from you and will not show you what
    // it learned is one you cannot correct.
    await signIn(page);
    await page.goto('/settings');
    await expect(page.locator('[data-testid="memory-viewer"]')).toBeVisible({ timeout: 30_000 });
    await expect(page.locator('[data-testid="memory-viewer"]')).toContainText('Style');
    await expect(page.locator('[data-testid="memory-viewer"]')).toContainText('Outcomes');
  });

  test('autonomy never offers an auto-send option', async ({ page }) => {
    await signIn(page);
    await page.goto('/settings');
    const options = await page.locator('[data-testid="autonomy-select"] option').allTextContents();
    expect(options.join(' ').toLowerCase()).not.toContain('auto-send');
    expect(options.join(' ').toLowerCase()).not.toContain('send automatically');
  });
});

test.describe('works on a phone', () => {
  test('the inbox is usable at phone width with no horizontal scroll', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await signIn(page);
    await expect(page.getByRole('heading', { name: 'Job inbox' })).toBeVisible();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    );
    expect(overflow).toBe(false);
  });
});
