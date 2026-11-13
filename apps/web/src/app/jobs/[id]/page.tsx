'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { api } from '@/lib/api/client';

interface JobDetail {
  job: {
    job_id: string;
    title: string;
    description: string;
    required_skills: string[];
    budget: { min: number; max: number; type: string };
    connects_cost: number;
    screening_questions: { text: string }[];
    proposals_received: number;
  };
  degraded: boolean;
  thread: {
    id: string;
    status: string;
    decision: string;
    fit_score: number;
    risk_score: number;
    price_usd: number;
    terminal_reason: string;
    summary: Record<string, unknown>;
  } | null;
  proposal: { id: string; status: string; word_count: number } | null;
  retrieval: Record<string, unknown>;
  node_timings: Record<string, { seconds: number; tokens: number }>;
}

export default function JobPage() {
  const { id } = useParams<{ id: string }>();
  const [data, setData] = useState<JobDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<JobDetail>(`/jobs/${id}`)
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : 'Could not load the job'));
  }, [id]);

  if (error)
    return (
      <div className="banner banner-bad" role="alert">
        {error}
      </div>
    );
  if (!data)
    return (
      <div className="card">
        <div className="skeleton" style={{ width: '50%', marginBottom: 10 }} />
        <div className="skeleton" style={{ width: '90%' }} />
      </div>
    );

  const { job, thread } = data;
  const matrix =
    (thread?.summary?.requirement_matches as
      | { requirement: string; satisfied: boolean; evidence_ids: string[]; gap_note: string }[]
      | undefined) ?? [];

  return (
    <>
      <div className="page-head">
        <h1>{job.title}</h1>
        <p>
          ${job.budget.min?.toLocaleString()}–${job.budget.max?.toLocaleString()} {job.budget.type}{' '}
          · {job.connects_cost} Connects · {job.proposals_received} competing proposals
        </p>
      </div>

      {thread && (
        <div className="grid grid-4" style={{ marginBottom: 14 }}>
          <div className="card">
            <div className="muted">Decision</div>
            <strong style={{ fontSize: 18, textTransform: 'capitalize' }}>
              {thread.decision || '—'}
            </strong>
          </div>
          <div className="card">
            <div className="muted">Fit</div>
            <strong style={{ fontSize: 18 }}>{(thread.fit_score * 100).toFixed(0)}%</strong>
            <div className="meter" style={{ marginTop: 6 }}>
              <span style={{ width: `${thread.fit_score * 100}%` }} />
            </div>
          </div>
          <div className="card">
            <div className="muted">Risk</div>
            <strong style={{ fontSize: 18 }}>{thread.risk_score.toFixed(2)}</strong>
          </div>
          <div className="card">
            <div className="muted">Bid</div>
            <strong style={{ fontSize: 18 }}>
              {thread.price_usd ? `$${thread.price_usd.toLocaleString()}` : '—'}
            </strong>
          </div>
        </div>
      )}

      {thread?.terminal_reason && (
        <div className="banner banner-info">
          <span>
            <strong>Why:</strong> {thread.terminal_reason}
          </span>
        </div>
      )}

      <div className="grid grid-2">
        <div className="card">
          <h3>The post</h3>
          <p style={{ whiteSpace: 'pre-wrap' }} className="muted">
            {job.description}
          </p>
          {job.screening_questions.length > 0 && (
            <>
              <h3 style={{ marginTop: 14 }}>Screening questions</h3>
              <ol className="muted">
                {job.screening_questions.map((q, i) => (
                  <li key={i}>{q.text}</li>
                ))}
              </ol>
            </>
          )}
          <div className="row" style={{ marginTop: 10 }}>
            {job.required_skills.map((s) => (
              <span key={s} className="badge">
                {s}
              </span>
            ))}
          </div>
        </div>

        <div>
          <div className="card">
            <h3>Requirement → evidence</h3>
            {matrix.length === 0 ? (
              <p className="muted">
                Run the analysis to see which requirements your portfolio can actually prove.
              </p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Requirement</th>
                    <th>Proof</th>
                  </tr>
                </thead>
                <tbody>
                  {matrix.map((row, i) => (
                    <tr key={i}>
                      <td>{row.requirement}</td>
                      <td>
                        {row.satisfied ? (
                          <span className="badge badge-good">
                            {row.evidence_ids.join(', ') || 'yes'}
                          </span>
                        ) : (
                          <span className="badge badge-warn" title={row.gap_note}>
                            gap
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {Object.keys(data.retrieval).length > 0 && (
            <div className="card">
              <h3>Retrieval</h3>
              <pre>{JSON.stringify(data.retrieval, null, 2).slice(0, 1400)}</pre>
            </div>
          )}

          {thread && (
            <div className="row">
              <Link className="btn" href={`/runs/${thread.id}`}>
                Open the trace
              </Link>
              {thread.status === 'awaiting_approval' && (
                <Link className="btn btn-primary" href={`/studio/${thread.id}`}>
                  Review the proposal
                </Link>
              )}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
