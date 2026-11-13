'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api/client';

interface Run {
  thread_id: string;
  kind: string;
  job_id: string;
  job_title: string;
  status: string;
  decision: string;
  fit_score: number;
  risk_score: number;
  tokens_used: number;
  cost_usd: number;
  latency_s: number;
  degraded: boolean;
  terminal_reason: string;
  created_at: string;
}

export default function RunsPage() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .get<{ runs: Run[] }>('/runs')
      .then((d) => setRuns(d.runs))
      .finally(() => setLoading(false));
  }, []);

  return (
    <>
      <div className="page-head">
        <h1>Agent runs</h1>
        <p>
          Every run GigBid has made, with its cost and what it decided. Open one to see which node
          reached which conclusion.
        </p>
      </div>

      {loading ? (
        <div className="card">
          <div className="skeleton" style={{ width: '60%' }} />
        </div>
      ) : runs.length === 0 ? (
        <div className="empty">
          <h3>No runs yet</h3>
          <Link className="btn btn-primary" href="/inbox">
            Analyse a job
          </Link>
        </div>
      ) : (
        <div className="card" style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>Job</th>
                <th>Status</th>
                <th>Decision</th>
                <th>Fit</th>
                <th>Risk</th>
                <th>Tokens</th>
                <th>Cost</th>
                <th>Latency</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.thread_id} data-testid="run-row">
                  <td style={{ maxWidth: 240 }}>
                    <Link href={`/runs/${run.thread_id}`}>
                      {run.job_title || run.job_id || run.thread_id.slice(0, 10)}
                    </Link>
                    {run.degraded && (
                      <span className="badge badge-warn" style={{ marginLeft: 6 }}>
                        degraded
                      </span>
                    )}
                  </td>
                  <td>
                    <span className="badge">{run.status}</span>
                  </td>
                  <td>
                    {run.decision && (
                      <span
                        className={`badge ${run.decision === 'bid' ? 'badge-good' : run.decision === 'watch' ? 'badge-warn' : ''}`}
                      >
                        {run.decision}
                      </span>
                    )}
                  </td>
                  <td>{run.fit_score ? `${(run.fit_score * 100).toFixed(0)}%` : '—'}</td>
                  <td>{run.risk_score ? run.risk_score.toFixed(2) : '—'}</td>
                  <td>{run.tokens_used.toLocaleString()}</td>
                  <td>${run.cost_usd.toFixed(5)}</td>
                  <td>{run.latency_s.toFixed(1)}s</td>
                  <td>
                    <Link className="btn btn-sm" href={`/runs/${run.thread_id}`}>
                      Trace
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
