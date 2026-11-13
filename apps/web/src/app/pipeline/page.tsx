'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api/client';

interface Proposal {
  id: string;
  thread_id: string;
  job_id: string;
  job_title: string;
  status: string;
  price_usd: number;
  word_count: number;
  connects_spent: number;
  unsupported_after: number;
  updated_at: string;
}

interface Approval {
  approval_id: string;
  thread_id: string;
  kind: string;
  created_at: string;
}

const COLUMNS: { key: string; label: string; statuses: string[] }[] = [
  { key: 'awaiting', label: 'Awaiting you', statuses: ['pending_approval', 'drafting', 'drafted'] },
  { key: 'submitted', label: 'Submitted', statuses: ['submitted', 'approved'] },
  { key: 'viewed', label: 'Viewed', statuses: ['viewed'] },
  { key: 'replied', label: 'Replied', statuses: ['replied'] },
  { key: 'interview', label: 'Interview', statuses: ['interview'] },
  { key: 'closed', label: 'Hired / lost', statuses: ['hired', 'lost', 'ignored', 'rejected'] },
];

export default function PipelinePage() {
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [p, a] = await Promise.all([
        api.get<{ proposals: Proposal[] }>('/proposals'),
        api.get<{ approvals: Approval[] }>('/approvals'),
      ]);
      setProposals(p.proposals);
      setApprovals(a.approvals);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function sync() {
    setSyncing(true);
    setNote(null);
    try {
      const result = await api.post<{ updated: number }>('/proposals/sync');
      setNote(
        result.updated > 0
          ? `${result.updated} proposal${result.updated === 1 ? '' : 's'} moved. Outcomes recorded.`
          : 'No changes on the marketplace since the last sync.',
      );
      await load();
    } catch (e) {
      setNote(e instanceof Error ? e.message : 'Sync failed');
    } finally {
      setSyncing(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>Pipeline</h1>
        <p>
          Every proposal, from waiting on you to hired. Outcomes feed the memory that shapes later
          drafts.
        </p>
      </div>

      <div className="row" style={{ marginBottom: 14 }}>
        <button onClick={sync} disabled={syncing}>
          {syncing ? 'Syncing…' : 'Sync from marketplace'}
        </button>
        <button onClick={load} disabled={loading}>
          Refresh
        </button>
      </div>

      {note && <div className="banner banner-info">{note}</div>}

      {approvals.length > 0 && (
        <div className="card" data-testid="pending-approvals">
          <h3>
            {approvals.length} waiting for your approval
          </h3>
          {approvals.map((a) => (
            <div className="spread" key={a.approval_id} style={{ marginTop: 8 }}>
              <span className="muted">
                {a.kind} · {new Date(a.created_at).toLocaleString()}
              </span>
              <Link className="btn btn-sm btn-primary" href={`/studio/${a.thread_id}`}>
                Review
              </Link>
            </div>
          ))}
        </div>
      )}

      {loading && proposals.length === 0 ? (
        <div className="card">
          <div className="skeleton" style={{ width: '60%' }} />
        </div>
      ) : proposals.length === 0 ? (
        <div className="empty">
          <h3>No proposals yet</h3>
          <p>Analyse a job from the inbox to get started.</p>
          <Link className="btn btn-primary" href="/inbox">
            Go to the inbox
          </Link>
        </div>
      ) : (
        <div className="kanban">
          {COLUMNS.map((column) => {
            const cards = proposals.filter((p) => column.statuses.includes(p.status));
            return (
              <div className="kanban-col" key={column.key}>
                <h4>
                  {column.label} ({cards.length})
                </h4>
                {cards.map((p) => (
                  <div className="card" key={p.id} data-testid="pipeline-card">
                    <strong style={{ fontSize: 14 }}>{p.job_title || p.job_id}</strong>
                    <div className="muted" style={{ marginTop: 4 }}>
                      ${p.price_usd.toLocaleString()} · {p.word_count} words
                      {p.connects_spent > 0 && ` · ${p.connects_spent} Connects`}
                    </div>
                    {p.unsupported_after > 0 && (
                      <span className="badge badge-bad">
                        {p.unsupported_after} unsupported
                      </span>
                    )}
                    <div className="row" style={{ marginTop: 8 }}>
                      <Link className="btn btn-sm" href={`/jobs/${p.job_id}`}>
                        Job
                      </Link>
                      <Link className="btn btn-sm" href={`/runs/${p.thread_id}`}>
                        Trace
                      </Link>
                    </div>
                  </div>
                ))}
                {cards.length === 0 && <div className="faint">—</div>}
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
