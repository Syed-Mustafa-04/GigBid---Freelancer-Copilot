'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { api, streamRun, type StreamEvent } from '@/lib/api/client';

interface Message {
  id: string;
  sender: string;
  body: string;
  intent: string;
  flagged: boolean;
  created_at: string;
}

export default function MessagesPage() {
  const [threads, setThreads] = useState<Record<string, Message[]>>({});
  const [loading, setLoading] = useState(true);
  const [draftFor, setDraftFor] = useState<string | null>(null);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.get<{ threads: Record<string, Message[]> }>('/messages');
      setThreads(data.threads);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function poll() {
    setNote(null);
    try {
      const result = await api.post<{ count: number }>('/messages/poll');
      setNote(
        result.count > 0
          ? `${result.count} new message${result.count === 1 ? '' : 's'}.`
          : 'No new messages.',
      );
      await load();
    } catch (e) {
      setNote(e instanceof Error ? e.message : 'Could not reach the marketplace');
    }
  }

  async function handle(threadId: string, body: string, jobId: string) {
    setDraftFor(threadId);
    setEvents([]);
    try {
      await streamRun(
        '/messages/handle',
        { thread_id: threadId, job_id: jobId, body },
        (event) => setEvents((prev) => [...prev, event]),
      );
      await load();
    } finally {
      setDraftFor(null);
    }
  }

  const entries = Object.entries(threads);

  return (
    <>
      <div className="page-head">
        <h1>Messages</h1>
        <p>
          Client threads. GigBid drafts a reply; you approve it before it is sent. A message that
          asks for something it should not is flagged, not answered.
        </p>
      </div>

      <div className="row" style={{ marginBottom: 14 }}>
        <button onClick={poll}>Check for new messages</button>
        <button onClick={load} disabled={loading}>
          Refresh
        </button>
      </div>

      {note && <div className="banner banner-info">{note}</div>}

      {draftFor && (
        <div className="card">
          <h3>Drafting a reply…</h3>
          {events
            .filter((e) => e.event === 'node_complete' || e.event === 'approval_required')
            .map((event, i) => (
              <div className="node-row" key={i}>
                <span className="node-dot" />
                <span className="node-name">
                  {event.event === 'approval_required' ? 'Waiting for you' : event.node}
                </span>
                <span className="muted">
                  {event.summary ? JSON.stringify(event.summary).slice(0, 100) : ''}
                </span>
              </div>
            ))}
        </div>
      )}

      {loading && entries.length === 0 ? (
        <div className="card">
          <div className="skeleton" style={{ width: '55%' }} />
        </div>
      ) : entries.length === 0 ? (
        <div className="empty">
          <h3>No client messages</h3>
          <p>
            Messages appear once a client replies to a submitted proposal. Advance the simulator or
            check for new messages.
          </p>
        </div>
      ) : (
        entries.map(([threadId, messages]) => {
          const latest = messages[0];
          return (
            <div className="card" key={threadId} data-testid="message-thread">
              <div className="card-head">
                <h3>Thread {threadId.slice(0, 12)}</h3>
                <div className="row">
                  {latest?.intent && <span className="badge">{latest.intent}</span>}
                  {latest?.flagged && <span className="badge badge-bad">flagged</span>}
                </div>
              </div>
              {messages.slice(0, 4).map((m) => (
                <div
                  key={m.id}
                  style={{
                    padding: '8px 10px',
                    borderRadius: 8,
                    background: 'var(--surface-2)',
                    marginBottom: 6,
                  }}
                >
                  <div className="faint">
                    {m.sender} · {new Date(m.created_at).toLocaleString()}
                  </div>
                  <div style={{ whiteSpace: 'pre-wrap' }}>{m.body}</div>
                </div>
              ))}
              <div className="row" style={{ marginTop: 8 }}>
                <button
                  className="btn-sm btn-primary"
                  disabled={!!draftFor}
                  onClick={() => handle(threadId, latest?.body ?? '', '')}
                  data-testid="draft-reply"
                >
                  Draft a reply
                </button>
                <Link className="btn btn-sm" href={`/studio/${threadId}`}>
                  Review draft
                </Link>
              </div>
            </div>
          );
        })
      )}
    </>
  );
}
