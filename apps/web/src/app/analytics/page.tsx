'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api/client';

interface Analytics {
  jobs_analysed: number;
  bids: number;
  skips: number;
  watches: number;
  proposals_drafted: number;
  proposals_submitted: number;
  reply_rate: number;
  hire_rate: number;
  connects_spent: number;
  connects_per_reply: number | null;
  by_category: Record<string, { submitted: number; replied: number; hired: number }>;
  cost_usd_total: number;
  cost_usd_per_job: number;
  tokens_total: number;
  latency_p50_s: number;
  latency_p95_s: number;
  unsupported_claims_before: number;
  unsupported_claims_after: number;
  top_rejection_reasons: [string, number][];
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card">
      <div className="muted">{label}</div>
      <strong style={{ fontSize: 20 }}>{value}</strong>
      {hint && <div className="faint">{hint}</div>}
    </div>
  );
}

export default function AnalyticsPage() {
  const [data, setData] = useState<Analytics | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<Analytics>('/analytics')
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : 'Could not load analytics'));
  }, []);

  if (error)
    return (
      <div className="banner banner-bad" role="alert">
        {error}
      </div>
    );
  if (!data)
    return (
      <div className="grid grid-4">
        {[0, 1, 2, 3].map((i) => (
          <div className="card" key={i}>
            <div className="skeleton" style={{ width: '70%' }} />
          </div>
        ))}
      </div>
    );

  const maxCategory = Math.max(
    1,
    ...Object.values(data.by_category).map((c) => c.submitted),
  );

  return (
    <>
      <div className="page-head">
        <h1>Analytics</h1>
        <p>
          What GigBid actually produced. Every number here comes from your own runs, not from a
          model scoring itself.
        </p>
      </div>

      <div className="grid grid-4">
        <Stat label="Jobs analysed" value={String(data.jobs_analysed)} />
        <Stat
          label="Bid / watch / skip"
          value={`${data.bids} / ${data.watches} / ${data.skips}`}
        />
        <Stat
          label="Reply rate"
          value={`${(data.reply_rate * 100).toFixed(0)}%`}
          hint={`${data.proposals_submitted} submitted`}
        />
        <Stat label="Hire rate" value={`${(data.hire_rate * 100).toFixed(0)}%`} />
      </div>

      <div className="grid grid-4">
        <Stat
          label="Connects spent"
          value={String(data.connects_spent)}
          hint={
            data.connects_per_reply
              ? `${data.connects_per_reply} per reply`
              : 'no replies yet'
          }
        />
        <Stat
          label="Cost per job"
          value={`$${data.cost_usd_per_job.toFixed(5)}`}
          hint={`$${data.cost_usd_total.toFixed(4)} total`}
        />
        <Stat
          label="Latency p95"
          value={`${data.latency_p95_s.toFixed(1)}s`}
          hint={`p50 ${data.latency_p50_s.toFixed(1)}s`}
        />
        <Stat label="Tokens" value={data.tokens_total.toLocaleString()} />
      </div>

      <div className="card" data-testid="claims-card">
        <h3>Unsupported claims</h3>
        <p className="muted">
          The claim verifier removed{' '}
          <strong>
            {data.unsupported_claims_before - data.unsupported_claims_after}
          </strong>{' '}
          unsupported claim
          {data.unsupported_claims_before - data.unsupported_claims_after === 1 ? '' : 's'} before
          anything reached you.
        </p>
        <div className="spread">
          <span className="muted">Before verification</span>
          <span className="badge badge-warn">{data.unsupported_claims_before}</span>
        </div>
        <div className="spread">
          <span className="muted">After verification (what you saw)</span>
          <span
            className={`badge ${data.unsupported_claims_after === 0 ? 'badge-good' : 'badge-bad'}`}
          >
            {data.unsupported_claims_after}
          </span>
        </div>
      </div>

      <div className="grid grid-2">
        <div className="card">
          <h3>By category</h3>
          {Object.keys(data.by_category).length === 0 ? (
            <p className="muted">No submitted proposals yet.</p>
          ) : (
            Object.entries(data.by_category).map(([name, c]) => (
              <div key={name} style={{ marginBottom: 10 }}>
                <div className="spread">
                  <span>{name}</span>
                  <span className="muted">
                    {c.replied}/{c.submitted} replied
                  </span>
                </div>
                <div className="meter">
                  <span style={{ width: `${(c.submitted / maxCategory) * 100}%` }} />
                </div>
              </div>
            ))
          )}
        </div>

        <div className="card">
          <h3>Why jobs were skipped</h3>
          {data.top_rejection_reasons.length === 0 ? (
            <p className="muted">Nothing skipped yet.</p>
          ) : (
            <ol className="muted" style={{ paddingLeft: 18 }}>
              {data.top_rejection_reasons.map(([reason, count]) => (
                <li key={reason} style={{ marginBottom: 6 }}>
                  {reason} <span className="badge">{count}</span>
                </li>
              ))}
            </ol>
          )}
        </div>
      </div>
    </>
  );
}
