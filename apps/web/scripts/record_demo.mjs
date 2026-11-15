/**
 * Records the GigBid demo walkthrough as a video file.
 *
 * Playwright's video capture records the viewport only, so there is no browser
 * chrome and no desktop clutter -- but also no mouse cursor. Every action is
 * therefore preceded by a visible outline on the target element, which both
 * tells a viewer where to look and gives a voiceover a beat to land on.
 *
 * Pacing is deliberate and slower than the app needs. The output is meant to
 * be narrated, so each step holds long enough to say a sentence over it.
 *
 * Usage:
 *   DEMO_BASE_URL=http://localhost:3000 DEMO_THREAD=t_xxxx node scripts/record_demo.mjs
 *
 * DEMO_THREAD is a thread id with a pending proposal approval. Pass one and the
 * recording includes the approval gate, the dry-run panel and the approval
 * itself -- the part of the demo the brief actually turns on. Without it the
 * recording covers everything else and reports the gate as skipped rather than
 * pretending.
 *
 * Output: docs/video/<timestamp>/*.webm
 */

import { chromium } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..', '..', '..'); // apps/web/scripts -> repo root

const BASE = process.env.DEMO_BASE_URL ?? 'http://localhost:3000';
const THREAD = process.env.DEMO_THREAD ?? '';
const EMAIL = process.env.REVIEWER_EMAIL ?? 'reviewer@gigbid.app';
const PASSWORD = process.env.REVIEWER_PASSWORD ?? 'gigbid-demo-2026';

const OUT = path.join(REPO, 'docs', 'video', new Date().toISOString().replace(/[:.]/g, '-'));
fs.mkdirSync(OUT, { recursive: true });

// Every hold is multiplied by PACE. The brief asks for three minutes and the
// default pacing lands around ninety seconds, so PACE=2 roughly doubles each
// beat without changing the sequence. A narrator needs room to finish a
// sentence over each step, and dead air is easier to cut than missing air is
// to add.
const PACE = Number(process.env.PACE ?? 1);
const beat = (ms) => new Promise((r) => setTimeout(r, Math.round(ms * PACE)));

async function focusOn(page, locator, { hold = 1400, click = false, label = '' } = {}) {
  try {
    await locator.scrollIntoViewIfNeeded({ timeout: 6000 });
    await locator.evaluate((el) => {
      el.style.transition = 'outline 160ms ease, box-shadow 160ms ease';
      el.style.outline = '3px solid #6ee7b7';
      el.style.outlineOffset = '3px';
      el.style.boxShadow = '0 0 0 9999px rgba(0,0,0,0.10)';
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
  } catch {
    if (label) console.log(`  (could not highlight ${label})`);
    return false;
  }
  await beat(hold);
  if (click) await locator.click({ timeout: 20000 }).catch(() => {});
  await locator
    .evaluate((el) => {
      el.style.outline = '';
      el.style.boxShadow = '';
    })
    .catch(() => {});
  return true;
}

async function caption(page, text, ms = 2800) {
  await page
    .evaluate((t) => {
      let bar = document.getElementById('__demo_caption');
      if (!bar) {
        bar = document.createElement('div');
        bar.id = '__demo_caption';
        Object.assign(bar.style, {
          position: 'fixed',
          left: '0',
          right: '0',
          bottom: '0',
          zIndex: '2147483647',
          padding: '14px 22px',
          font: '600 17px/1.45 ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif',
          color: '#ecfdf5',
          background: 'linear-gradient(to top, rgba(2,6,23,0.97), rgba(2,6,23,0.80))',
          borderTop: '2px solid #34d399',
          letterSpacing: '0.01em',
          transition: 'opacity 200ms ease',
        });
        document.body.appendChild(bar);
      }
      bar.textContent = t;
      bar.style.opacity = '1';
    }, text)
    .catch(() => {});
  await beat(ms);
}

async function clearCaption(page) {
  await page
    .evaluate(() => {
      const bar = document.getElementById('__demo_caption');
      if (bar) bar.style.opacity = '0';
    })
    .catch(() => {});
}

const steps = [];
const record = (name, ok, note = '') => {
  steps.push({ name, ok, note });
  console.log(`${ok ? 'OK  ' : 'SKIP'} ${name}${note ? ` -- ${note}` : ''}`);
};

async function main() {
  console.log(`Recording against ${BASE}`);
  console.log(`Approval thread: ${THREAD || '(none supplied)'}`);
  console.log(`Output -> ${OUT}\n`);

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    recordVideo: { dir: OUT, size: { width: 1280, height: 720 } },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(45000);

  try {
    // -- 1. Sign in ---------------------------------------------------------
    await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded', timeout: 120000 });
    await beat(1600);
    await caption(page, 'GigBid - a multi-agent freelancer copilot. Ten agents on a LangGraph state machine.', 3400);

    const email = page.locator('input[type="email"], input[name="email"]').first();
    const pw = page.locator('input[type="password"], input[name="password"]').first();
    await focusOn(page, email, { hold: 700 });
    await email.fill(EMAIL, { timeout: 15000 });
    await beat(600);
    await pw.fill(PASSWORD, { timeout: 15000 });
    await beat(900);
    await focusOn(page, page.locator('[data-testid="submit-login"]'), { hold: 900, click: true });
    await page.waitForURL(/inbox|\/$/, { timeout: 60000 }).catch(() => {});
    await beat(2600);
    record('sign in', !page.url().includes('/login'));

    // -- 2. Job inbox -------------------------------------------------------
    await page.goto(`${BASE}/inbox`, { waitUntil: 'domcontentloaded' });
    await page.locator('[data-testid="job-card"]').first().waitFor({ timeout: 60000 }).catch(() => {});
    await clearCaption(page);
    await caption(page, 'The job inbox. Every job is scored for fit and risk before a single Connect is spent.', 3800);
    const cards = page.locator('[data-testid="job-card"]');
    const cardCount = await cards.count().catch(() => 0);
    record('job inbox', cardCount > 0, `${cardCount} cards`);
    for (let i = 0; i < Math.min(3, cardCount); i++) {
      await focusOn(page, cards.nth(i), { hold: 1800 });
    }

    await caption(page, 'Connects are finite and spending them is irreversible, so the decision to bid is itself evaluated.', 3800);
    await beat(600);

    // -- 3. The scam job ----------------------------------------------------
    await caption(page, 'A scam-shaped post - an entire SaaS platform for fifty dollars - is flagged, not bid on.', 3800);
    const scam = page.getByText(/budget 50 USD|entire SaaS platform/i).first();
    await focusOn(page, scam, { hold: 2800, label: 'scam job' });

    // -- 4. Analyse, streaming node by node ---------------------------------
    await clearCaption(page);
    await caption(page, 'Analysing. Three agents run in parallel and each reports as it finishes - real progress, not a spinner.', 4200);
    const analyse = page.locator('[data-testid="analyse-job"]').first();
    const analysed = await focusOn(page, analyse, { hold: 1200, click: true, label: 'analyse button' });
    if (analysed) {
      const progress = page.locator('[data-testid="run-progress"]');
      await progress.waitFor({ timeout: 30000 }).catch(() => {});
      for (let i = 0; i < 8; i++) {
        await beat(2500);
        if (!(await progress.isVisible().catch(() => false))) break;
      }
      await beat(1500);
      await caption(page, 'This one was skipped, and the flags that caused it are shown rather than hidden behind a score.', 3800);
    }
    record('analysis streamed', analysed);

    // -- 5. The approval gate ----------------------------------------------
    let gateShown = false;
    if (THREAD) {
      await clearCaption(page);
      await page.goto(`${BASE}/studio/${THREAD}`, { waitUntil: 'domcontentloaded' });
      const panel = page.locator('[data-testid="approval-panel"]');
      // waitFor, not isVisible: isVisible() takes no timeout and answers
      // immediately, so it reports false simply because the page has not
      // finished rendering, and the gate gets silently skipped.
      gateShown = await panel
        .waitFor({ state: 'visible', timeout: 45000 })
        .then(() => true)
        .catch(() => false);
      if (gateShown) {
        await caption(page, 'A job worth bidding on. Execution stops here - nothing is sent until a human approves it.', 4200);
        await focusOn(page, panel, { hold: 3400 });

        const dryRun = page.locator('[data-testid="dry-run"]');
        if (await dryRun.isVisible().catch(() => false)) {
          await caption(page, 'The dry run shows the exact Connects cost and the idempotency key before anything is spent.', 4200);
          await focusOn(page, dryRun, { hold: 3600 });
        }

        await caption(page, 'Approving spends Connects. That key makes submission exactly-once: approve twice, it spends once.', 4200);
        const approve = page.locator('[data-testid="approve"]');
        if (await approve.isEnabled().catch(() => false)) {
          await focusOn(page, approve, { hold: 1600, click: true });
          await page
            .locator('[data-testid="approval-resolved"]')
            .waitFor({ timeout: 60000 })
            .catch(() => {});
          await beat(2600);
          await caption(page, 'Submitted. The Connects are spent once, and the pipeline moves.', 3400);
          await beat(1800);
        }
      }
    }
    record(
      'approval gate',
      gateShown,
      gateShown
        ? 'shown and approved'
        : THREAD
          ? `panel never became visible on /studio/${THREAD}`
          : 'no DEMO_THREAD supplied',
    );

    // -- 6. Pipeline --------------------------------------------------------
    await clearCaption(page);
    await page.goto(`${BASE}/pipeline`, { waitUntil: 'domcontentloaded' }).catch(() => {});
    await beat(2400);
    await caption(page, 'Every proposal is tracked through to its outcome.', 3200);
    await beat(1800);
    record('pipeline', true);

    // -- 7. Analytics -------------------------------------------------------
    await page.goto(`${BASE}/analytics`, { waitUntil: 'domcontentloaded' }).catch(() => {});
    await beat(2400);
    await caption(page, 'Outcomes are measured, so the system can be judged on results rather than vibes.', 3600);
    await beat(2000);
    record('analytics', true);

    // -- 8. Memory ----------------------------------------------------------
    await page.goto(`${BASE}/settings`, { waitUntil: 'domcontentloaded' }).catch(() => {});
    await beat(2000);
    const memory = page.locator('[data-testid="memory-viewer"]');
    const hasMemory = await memory.isVisible().catch(() => false);
    if (hasMemory) {
      await caption(page, 'What the system remembers is inspectable and editable by the person it belongs to.', 3800);
      await focusOn(page, memory, { hold: 3200 });
    }
    record('memory viewer', hasMemory);

    await clearCaption(page);
    await beat(1400);
  } catch (err) {
    console.error('\nRecording hit an error (the video up to this point is still saved):');
    console.error(err?.message ?? err);
  } finally {
    await context.close(); // flushes the video file
    await browser.close();
  }

  const files = fs.readdirSync(OUT).filter((f) => f.endsWith('.webm'));
  console.log('\n--- summary ---');
  for (const s of steps) console.log(`${s.ok ? 'OK  ' : 'SKIP'} ${s.name}${s.note ? ` (${s.note})` : ''}`);
  console.log(`\nvideo files: ${files.length}`);
  for (const f of files) {
    const p = path.join(OUT, f);
    console.log(`  ${p}  (${(fs.statSync(p).size / 1e6).toFixed(1)} MB)`);
  }
}

main();
