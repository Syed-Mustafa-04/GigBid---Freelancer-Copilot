'use client';

import { useState } from 'react';
import { api, type ApprovalPayload } from '@/lib/api/client';
import { ClaimAnnotatedText, ClaimLegend } from './EvidenceHover';

/**
 * The approval gate (R4 / C5).
 *
 * Everything needed to decide is on this one screen: the draft with every
 * claim's evidence, what the Critic objected to, what Compliance checked, the
 * price rationale against the market band, the risk report, and the dry-run
 * preview of the exact call that would be made and what it costs.
 *
 * Editing invalidates the earlier compliance verdict - the API re-runs it -
 * which is why the edit path is a different button rather than a silent
 * mutation before approve.
 */
export function ApprovalPanel({
  payload,
  onResolved,
}: {
  payload: ApprovalPayload;
  onResolved: (result: { status?: string; terminal_reason?: string }) => void;
}) {
  const draft = payload.draft;
  const reply = payload.reply;
  const [letter, setLetter] = useState(draft?.cover_letter ?? reply?.body ?? '');
  const [price, setPrice] = useState(draft?.price_usd ?? 0);
  const [answers, setAnswers] = useState(draft?.screening_answers ?? []);
  const [editing, setEditing] = useState(false);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const original = draft?.cover_letter ?? reply?.body ?? '';
  const dirty = letter !== original || price !== (draft?.price_usd ?? 0);

  async function respond(action: 'approve' | 'edit' | 'reject') {
    setBusy(action);
    setError(null);
    try {
      const body: Record<string, unknown> = { action, reason };
      if (action === 'edit') {
        if (draft) {
          body.edited_cover_letter = letter;
          body.edited_price_usd = price;
          body.edited_screening_answers = answers;
        } else {
          body.edited_reply_body = letter;
        }
      }
      const result = await api.post<{ status?: string; terminal_reason?: string }>(
        `/approvals/${payload.thread_id}`,
        body,
      );
      onResolved(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong');
    } finally {
      setBusy(null);
    }
  }

  const blocking = payload.compliance?.violations.filter((v) => v.severity === 'block') ?? [];

  return (
    <div data-testid="approval-panel">
      {blocking.length > 0 && (
        <div className="banner banner-bad" role="alert">
          <strong>Blocked by the compliance guard.</strong>
          <span>This cannot be sent until the flagged text is changed.</span>
        </div>
      )}

      <div className="grid grid-2">
        {/* ------------------------------------------------ draft column */}
        <div>
          <div className="card">
            <div className="card-head">
              <h3>{draft ? 'Proposal draft' : 'Reply draft'}</h3>
              <div className="row">
                {draft && <span className="badge">{draft.word_count} words</span>}
                {draft && draft.revision > 0 && (
                  <span className="badge">revision {draft.revision}</span>
                )}
                <button className="btn-sm" onClick={() => setEditing((v) => !v)}>
                  {editing ? 'Preview' : 'Edit'}
                </button>
              </div>
            </div>

            {editing ? (
              <>
                <div className="field">
                  <label htmlFor="letter">Cover letter</label>
                  <textarea
                    id="letter"
                    data-testid="edit-cover-letter"
                    value={letter}
                    onChange={(e) => setLetter(e.target.value)}
                    rows={12}
                  />
                </div>
                {draft && (
                  <div className="field">
                    <label htmlFor="price">Bid amount (USD)</label>
                    <input
                      id="price"
                      type="number"
                      value={price}
                      onChange={(e) => setPrice(Number(e.target.value))}
                    />
                  </div>
                )}
                {answers.map((answer, i) => (
                  <div className="field" key={i}>
                    <label htmlFor={`answer-${i}`}>{answer.question}</label>
                    <textarea
                      id={`answer-${i}`}
                      rows={3}
                      value={answer.answer}
                      onChange={(e) => {
                        const next = [...answers];
                        next[i] = { ...answer, answer: e.target.value };
                        setAnswers(next);
                      }}
                    />
                  </div>
                ))}
              </>
            ) : (
              <>
                <ClaimAnnotatedText
                  text={letter}
                  claims={payload.claims}
                  evidence={payload.evidence}
                />
                <ClaimLegend />
                {answers.length > 0 && (
                  <div style={{ marginTop: 16 }}>
                    <h3>Screening answers</h3>
                    {answers.map((answer, i) => (
                      <div key={i} style={{ marginBottom: 10 }}>
                        <div className="muted">{answer.question}</div>
                        <div>{answer.answer || <em className="faint">(not answered)</em>}</div>
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>

          {/* ---------------------------------------------- critic notes */}
          {payload.critique && payload.critique.issues.length > 0 && (
            <div className="card">
              <h3>Critic</h3>
              <p className="muted">
                {payload.critique.approved
                  ? 'Approved, with notes.'
                  : 'Sent back; the revision limit was reached.'}{' '}
                Score {(payload.critique.score * 100).toFixed(0)}%.
              </p>
              <ul className="muted" style={{ margin: 0, paddingLeft: 18 }}>
                {payload.critique.issues.map((issue, i) => (
                  <li key={i}>
                    <span
                      className={`badge ${
                        issue.severity === 'blocking'
                          ? 'badge-bad'
                          : issue.severity === 'major'
                            ? 'badge-warn'
                            : ''
                      }`}
                    >
                      {issue.kind}
                    </span>{' '}
                    {issue.detail}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {payload.compliance && payload.compliance.violations.length > 0 && (
            <div className="card">
              <h3>Compliance</h3>
              {payload.compliance.violations.map((v, i) => (
                <div key={i} style={{ marginBottom: 8 }}>
                  <span className={`badge ${v.severity === 'block' ? 'badge-bad' : 'badge-warn'}`}>
                    {v.rule_id}
                  </span>
                  {v.evidence_span && (
                    <div className="muted" style={{ marginTop: 4 }}>
                      “{v.evidence_span}”
                    </div>
                  )}
                  <div className="faint">{v.remedy}</div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* ------------------------------------------------ context column */}
        <div>
          {payload.price_quote && (
            <div className="card">
              <h3>Price</h3>
              <div className="spread">
                <strong style={{ fontSize: 22 }}>
                  ${payload.price_quote.amount_usd.toLocaleString()}
                </strong>
                <span
                  className={`badge ${payload.price_quote.inside_fair_band ? 'badge-good' : 'badge-warn'}`}
                >
                  {payload.price_quote.inside_fair_band ? 'inside fair band' : 'outside band'}
                </span>
              </div>
              <div className="muted" style={{ marginTop: 8 }}>
                Market p25 ${payload.price_quote.fair_band_low.toLocaleString()} · median $
                {payload.price_quote.market_median.toLocaleString()} · p75 $
                {payload.price_quote.fair_band_high.toLocaleString()}
              </div>
              <div className="muted">
                Effective rate ${payload.price_quote.effective_hourly_usd.toLocaleString()}/hour
              </div>
              <p className="muted" style={{ marginTop: 8 }}>
                {payload.price_quote.rationale}
              </p>
            </div>
          )}

          {payload.risk_report && (
            <div className="card">
              <h3>Risk</h3>
              <div className="spread">
                <span className="muted">Score</span>
                <span
                  className={`badge ${
                    payload.risk_report.score > 0.6
                      ? 'badge-bad'
                      : payload.risk_report.score > 0.3
                        ? 'badge-warn'
                        : 'badge-good'
                  }`}
                >
                  {payload.risk_report.score.toFixed(2)}
                </span>
              </div>
              {payload.risk_report.flags.length > 0 && (
                <div className="row" style={{ marginTop: 8 }}>
                  {payload.risk_report.flags.map((f) => (
                    <span key={f} className="badge badge-warn">
                      {f}
                    </span>
                  ))}
                </div>
              )}
              {payload.risk_report.injection_detected && (
                <div className="banner banner-warn" style={{ marginTop: 10 }}>
                  <span>
                    This job post contained an instruction aimed at an automated assistant. It was
                    ignored. The job was judged on its own merits.
                  </span>
                </div>
              )}
              <p className="muted">{payload.risk_report.explanation}</p>
            </div>
          )}

          {/* --------------------------------------------- dry run preview */}
          {payload.dry_run && (
            <div className="card" data-testid="dry-run">
              <h3>What will be sent</h3>
              <table>
                <tbody>
                  <tr>
                    <th>Tool</th>
                    <td>
                      <code>{payload.dry_run.tool_name}</code>
                    </td>
                  </tr>
                  <tr>
                    <th>Server</th>
                    <td>{payload.dry_run.server}</td>
                  </tr>
                  <tr>
                    <th>Connects</th>
                    <td>
                      <strong>{payload.dry_run.connects_cost}</strong>{' '}
                      <span className="faint">(cannot be refunded)</span>
                    </td>
                  </tr>
                  <tr>
                    <th>Idempotency key</th>
                    <td>
                      <code>{payload.dry_run.idempotency_key.slice(0, 16)}…</code>
                    </td>
                  </tr>
                </tbody>
              </table>
              <details style={{ marginTop: 10 }}>
                <summary className="muted" style={{ cursor: 'pointer' }}>
                  Raw payload
                </summary>
                <pre>{JSON.stringify(payload.dry_run.arguments, null, 2)}</pre>
              </details>
            </div>
          )}

          <div className="card">
            <h3>This run</h3>
            <div className="muted">
              {Object.entries(payload.budget_snapshot).map(([key, value]) => (
                <div key={key} className="spread">
                  <span>{key.replace(/_/g, ' ')}</span>
                  <span>{typeof value === 'number' ? value.toLocaleString() : String(value)}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* ------------------------------------------------------ actions */}
      <div className="card" style={{ position: 'sticky', bottom: 0 }}>
        {error && (
          <div className="banner banner-bad" role="alert">
            {error}
          </div>
        )}
        <div className="field">
          <label htmlFor="reason">Note (optional; a rejection reason is kept for later)</label>
          <input
            id="reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Why are you approving, editing or rejecting this?"
            style={{ minHeight: 'auto' }}
          />
        </div>
        <div className="row">
          <button
            className="btn-primary"
            data-testid="approve"
            disabled={!!busy || blocking.length > 0 || dirty}
            onClick={() => respond('approve')}
            title={
              dirty
                ? 'You have unsaved edits. Use "Save edits and approve".'
                : blocking.length > 0
                  ? 'Blocked by compliance.'
                  : undefined
            }
          >
            {busy === 'approve' ? 'Submitting…' : 'Approve and submit'}
          </button>
          <button
            data-testid="approve-edited"
            disabled={!!busy || !dirty}
            onClick={() => respond('edit')}
          >
            {busy === 'edit' ? 'Saving…' : 'Save edits and approve'}
          </button>
          <button
            className="btn-danger"
            data-testid="reject"
            disabled={!!busy}
            onClick={() => respond('reject')}
          >
            {busy === 'reject' ? 'Rejecting…' : 'Reject'}
          </button>
        </div>
        {dirty && (
          <p className="faint" style={{ marginTop: 8 }}>
            Edits re-run the compliance check before anything is sent, and recur across proposals
            they become a style rule you can see and delete in Settings.
          </p>
        )}
      </div>
    </div>
  );
}
