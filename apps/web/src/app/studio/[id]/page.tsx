'use client';

import { useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { api, type ApprovalPayload } from '@/lib/api/client';
import { ApprovalPanel } from '@/components/ApprovalPanel';

/**
 * Proposal studio — the approval screen.
 *
 * The pending approval is fetched from the server rather than held in client
 * state, so a page refresh never loses it: the interrupt lives in the
 * checkpointer and this screen is just a view onto it.
 */
export default function StudioPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [payload, setPayload] = useState<ApprovalPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resolved, setResolved] = useState<{ status?: string; terminal_reason?: string } | null>(
    null,
  );

  useEffect(() => {
    api
      .get<{ payload: ApprovalPayload }>(`/approvals/${id}`)
      .then((data) => setPayload(data.payload))
      .catch((e) => setError(e instanceof Error ? e.message : 'No pending approval'));
  }, [id]);

  if (resolved) {
    return (
      <>
        <div className="page-head">
          <h1>Done</h1>
        </div>
        <div className="card" data-testid="approval-resolved">
          <h3 style={{ textTransform: 'capitalize' }}>{resolved.status ?? 'resolved'}</h3>
          <p className="muted">{resolved.terminal_reason}</p>
          <div className="row">
            <button className="btn-primary" onClick={() => router.push('/pipeline')}>
              Back to the pipeline
            </button>
            <button onClick={() => router.push('/inbox')}>Next job</button>
          </div>
        </div>
      </>
    );
  }

  if (error) {
    return (
      <>
        <div className="page-head">
          <h1>Proposal studio</h1>
        </div>
        <div className="empty">
          <h3>Nothing waiting here</h3>
          <p className="muted">{error}</p>
          <button onClick={() => router.push('/pipeline')}>Back to the pipeline</button>
        </div>
      </>
    );
  }

  if (!payload) {
    return (
      <div className="card">
        <div className="skeleton" style={{ width: '40%', marginBottom: 10 }} />
        <div className="skeleton" style={{ width: '95%', marginBottom: 6 }} />
        <div className="skeleton" style={{ width: '85%' }} />
      </div>
    );
  }

  return (
    <>
      <div className="page-head">
        <h1>Review before anything is sent</h1>
        <p>
          Nothing here has reached the client. Hover any claim to see the evidence behind it. Edits
          re-run the compliance check.
        </p>
      </div>
      <ApprovalPanel payload={payload} onResolved={setResolved} />
    </>
  );
}
