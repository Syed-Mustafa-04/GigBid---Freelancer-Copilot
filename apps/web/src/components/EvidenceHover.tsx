'use client';

import { useState } from 'react';
import type { Claim, EvidenceItem } from '@/lib/api/client';

/**
 * Renders a proposal with every claim marked by its grounding status, and the
 * evidence behind it on hover or focus.
 *
 * This is the screen that makes R5 inspectable rather than merely asserted: a
 * user can see which sentences are backed by what, which were weakened, and
 * which were deleted. Keyboard focus triggers it too - hover-only would make
 * the grounding evidence unreachable without a mouse.
 */
export function ClaimAnnotatedText({
  text,
  claims,
  evidence,
}: {
  text: string;
  claims: Claim[];
  evidence: EvidenceItem[];
}) {
  const [open, setOpen] = useState<string | null>(null);
  const byId = new Map(evidence.map((e) => [e.evidence_id, e]));

  // Claims are matched against the live text; a claim that was rewritten
  // appears under its new wording, and a removed one is listed separately.
  const live = claims.filter((c) => !c.removed);
  const removed = claims.filter((c) => c.removed);

  const segments: { text: string; claim: Claim | null }[] = [];
  let remaining = text;

  for (const claim of live) {
    const needle = claim.rewritten_as ?? claim.text;
    const index = remaining.indexOf(needle);
    if (index === -1) continue;
    if (index > 0) segments.push({ text: remaining.slice(0, index), claim: null });
    segments.push({ text: needle, claim });
    remaining = remaining.slice(index + needle.length);
  }
  if (remaining) segments.push({ text: remaining, claim: null });
  if (segments.length === 0) segments.push({ text, claim: null });

  return (
    <div>
      <p style={{ whiteSpace: 'pre-wrap', margin: 0, lineHeight: 1.7 }}>
        {segments.map((segment, i) =>
          segment.claim ? (
            <span
              key={`${segment.claim.claim_id}-${i}`}
              className={`claim claim-${segment.claim.status.replace('_supported', '')}`}
              tabIndex={0}
              role="button"
              aria-expanded={open === segment.claim.claim_id}
              aria-label={`Claim, ${segment.claim.status.replace('_', ' ')}. Show evidence.`}
              data-testid={`claim-${segment.claim.status}`}
              onMouseEnter={() => setOpen(segment.claim!.claim_id)}
              onMouseLeave={() => setOpen(null)}
              onFocus={() => setOpen(segment.claim!.claim_id)}
              onBlur={() => setOpen(null)}
            >
              {segment.text}
              {open === segment.claim.claim_id && (
                <span className="evidence-pop" role="tooltip">
                  <strong style={{ textTransform: 'capitalize' }}>
                    {segment.claim.status.replace('_', ' ')}
                  </strong>{' '}
                  <span className="faint">
                    (support {(segment.claim.support_score * 100).toFixed(0)}%)
                  </span>
                  <div className="muted" style={{ margin: '6px 0' }}>
                    {segment.claim.verifier_note}
                  </div>
                  {segment.claim.rewritten_as && (
                    <div className="muted" style={{ marginBottom: 6 }}>
                      Rewritten from: <em>{segment.claim.text}</em>
                    </div>
                  )}
                  {segment.claim.evidence_ids.length === 0 ? (
                    <div className="faint">No evidence cited.</div>
                  ) : (
                    segment.claim.evidence_ids.map((id) => {
                      const item = byId.get(id);
                      return (
                        <div
                          key={id}
                          style={{
                            borderTop: '1px solid var(--border)',
                            paddingTop: 6,
                            marginTop: 6,
                          }}
                        >
                          <code>{id}</code>{' '}
                          <span className="faint">{item?.type ?? 'unknown'}</span>
                          <div style={{ marginTop: 2 }}>{item?.title ?? '(not retrieved)'}</div>
                          {item && (
                            <div className="muted" style={{ marginTop: 4 }}>
                              {item.text.slice(0, 260)}
                              {item.text.length > 260 ? '…' : ''}
                            </div>
                          )}
                        </div>
                      );
                    })
                  )}
                </span>
              )}
            </span>
          ) : (
            <span key={`plain-${i}`}>{segment.text}</span>
          ),
        )}
      </p>

      {removed.length > 0 && (
        <details style={{ marginTop: 14 }}>
          <summary className="muted" style={{ cursor: 'pointer' }}>
            {removed.length} claim{removed.length === 1 ? '' : 's'} removed by the verifier
          </summary>
          <ul className="muted" style={{ marginTop: 8 }}>
            {removed.map((claim) => (
              <li key={claim.claim_id} style={{ marginBottom: 6 }}>
                <span style={{ textDecoration: 'line-through' }}>{claim.text}</span>
                <div className="faint">{claim.verifier_note}</div>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export function ClaimLegend() {
  return (
    <div className="row faint" style={{ marginTop: 10 }}>
      <span className="claim claim-supported">supported</span>
      <span className="claim claim-partly">weakened to what the evidence shows</span>
      <span className="claim claim-unsupported">removed</span>
      <span>— hover or focus any claim to see its evidence</span>
    </div>
  );
}
