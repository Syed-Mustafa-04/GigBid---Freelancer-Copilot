'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { api, streamRun, type Job, type StreamEvent } from '@/lib/api/client';

const NODE_LABELS: Record<string, string> = {
  analyst: 'Reading the post',
  risk_assessor: 'Checking the client',
  fit_matcher: 'Matching your portfolio',
  join: 'Merging analyses',
  decide: 'Deciding bid or skip',
  pricing: 'Pricing the work',
  writer: 'Drafting',
  verify_claims: 'Verifying every claim',
  critic: 'Reviewing the draft',
  compliance: 'Checking policy',
  approval: 'Waiting for you',
  submit: 'Submitting',
  finalise_skip: 'Skipped',
  finalise_blocked: 'Blocked',
};

export default function InboxPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [degraded, setDegraded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [running, setRunning] = useState<string | null>(null);
  const [progress, setProgress] = useState<StreamEvent[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.get<{ jobs: Job[]; degraded: boolean }>(
        `/jobs?limit=30&query=${encodeURIComponent(query)}`,
      );
      setJobs(data.jobs);
      setDegraded(data.degraded);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load jobs');
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    void load();
  }, [load]);

  async function analyse(jobId: string) {
    setRunning(jobId);
    setProgress([]);
    try {
      await streamRun('/jobs/analyse', { job_id: jobId }, (event) => {
        setProgress((prev) => [...prev, event]);
      });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The run failed to start');
    } finally {
      setRunning(null);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>Job inbox</h1>
        <p>
          New jobs ranked by fit. Nothing here has been sent anywhere — analysing a job costs no
          Connects.
        </p>
      </div>

      <div className="row" style={{ marginBottom: 16 }}>
        <input
          aria-label="Search jobs"
          placeholder="Search jobs…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && load()}
          style={{ maxWidth: 360 }}
        />
        <button onClick={load} disabled={loading}>
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {error && (
        <div className="banner banner-bad" role="alert">
          {error}
        </div>
      )}

      {running && (
        <div className="card" data-testid="run-progress">
          <h3>Analysing…</h3>
          {progress
            .filter((e) => e.event === 'node_complete' || e.event === 'approval_required')
            .map((event, i) => (
              <div className="node-row" key={i}>
                <span className="node-dot" />
                <span className="node-name">
                  {event.event === 'approval_required'
                    ? 'Waiting for you'
                    : (NODE_LABELS[event.node ?? ''] ?? event.node)}
                </span>
                <span className="muted">
                  {event.summary ? JSON.stringify(event.summary).slice(0, 110) : ''}
                </span>
              </div>
            ))}
          {progress.length === 0 && <div className="skeleton" style={{ width: '60%' }} />}
        </div>
      )}

      {loading && jobs.length === 0 ? (
        <>
          {[0, 1, 2].map((i) => (
            <div className="card" key={i}>
              <div className="skeleton" style={{ width: '45%', marginBottom: 8 }} />
              <div className="skeleton" style={{ width: '80%' }} />
            </div>
          ))}
        </>
      ) : jobs.length === 0 ? (
        <div className="empty">
          <h3>{degraded ? 'The marketplace is not responding' : 'No jobs match'}</h3>
          <p>
            {degraded
              ? 'This is not an empty result — GigBid cannot reach the marketplace right now.'
              : 'Try a broader search, or widen your saved searches in Settings.'}
          </p>
        </div>
      ) : (
        jobs.map((job) => {
          const g = job.gigbid;
          return (
            <article className="card" key={job.job_id} data-testid="job-card">
              <div className="card-head">
                <div style={{ minWidth: 0 }}>
                  <h3>
                    <Link href={`/jobs/${job.job_id}`}>{job.title}</Link>
                  </h3>
                  <div className="muted">
                    ${job.budget.min?.toLocaleString()}–${job.budget.max?.toLocaleString()}{' '}
                    {job.budget.type} · {job.connects_cost} Connects ·{' '}
                    {job.proposals_received} proposals
                  </div>
                </div>
                <div className="row">
                  {g.decision && (
                    <span
                      className={`badge ${
                        g.decision === 'bid'
                          ? 'badge-good'
                          : g.decision === 'watch'
                            ? 'badge-warn'
                            : ''
                      }`}
                      data-testid={`decision-${g.decision}`}
                    >
                      {g.decision}
                    </span>
                  )}
                  {g.fit_score !== null && (
                    <span className="badge">fit {(g.fit_score * 100).toFixed(0)}%</span>
                  )}
                  {g.risk_score !== null && g.risk_score > 0.4 && (
                    <span className="badge badge-bad">risk {g.risk_score.toFixed(2)}</span>
                  )}
                </div>
              </div>

              <p className="muted" style={{ marginTop: 8 }}>
                {job.description.slice(0, 220)}
                {job.description.length > 220 ? '…' : ''}
              </p>

              <div className="row" style={{ marginTop: 10 }}>
                {g.status === 'not_analysed' ? (
                  <button
                    className="btn-primary btn-sm"
                    disabled={!!running}
                    onClick={() => analyse(job.job_id)}
                    data-testid="analyse-job"
                  >
                    {running === job.job_id ? 'Analysing…' : 'Analyse'}
                  </button>
                ) : (
                  <Link className="btn btn-sm" href={`/jobs/${job.job_id}`}>
                    View analysis
                  </Link>
                )}
                {g.status === 'awaiting_approval' && (
                  <Link className="btn btn-sm btn-primary" href={`/studio/${g.thread_id}`}>
                    Review proposal
                  </Link>
                )}
                {g.price_usd ? (
                  <span className="badge">bid ${g.price_usd.toLocaleString()}</span>
                ) : null}
                {job.required_skills.slice(0, 3).map((s) => (
                  <span key={s} className="badge">
                    {s}
                  </span>
                ))}
              </div>
            </article>
          );
        })
      )}
    </>
  );
}
