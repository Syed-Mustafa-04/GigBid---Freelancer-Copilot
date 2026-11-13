'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { api } from '@/lib/api/client';

interface ToolCall {
  agent: string;
  server: string;
  tool: string;
  arguments: Record<string, unknown>;
  result_preview: string;
  latency_s: number;
  ok: boolean;
  error: string;
}

interface TraceEntry {
  node: string;
  ts: number;
  seconds: number;
  llm_calls: { node: string; model: string; prompt_tokens: number; completion_tokens: number; latency_s: number; cost_usd: number }[];
  tool_calls: ToolCall[];
}

interface RunDetail {
  thread_id: string;
  status: string;
  job_title: string;
  terminal_reason: string;
  trace: TraceEntry[];
  node_timings: Record<string, { seconds: number; tokens: number; cost_usd: number; prompt_version: number | null }>;
  retrieval: Record<string, unknown>;
  totals: { tokens: number; cost_usd: number; latency_s: number };
}

export default function RunDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(null);

  useEffect(() => {
    api
      .get<RunDetail>(`/runs/${id}`)
      .then(setRun)
      .catch((e) => setError(e instanceof Error ? e.message : 'Trace not found'));
  }, [id]);

  if (error)
    return (
      <div className="banner banner-bad" role="alert">
        {error}
      </div>
    );
  if (!run)
    return (
      <div className="card">
        <div className="skeleton" style={{ width: '50%' }} />
      </div>
    );

  return (
    <>
      <div className="page-head">
        <h1>Trace</h1>
        <p>
          {run.job_title || run.thread_id} · {run.status}
        </p>
      </div>

      <div className="grid grid-3" style={{ marginBottom: 14 }}>
        <div className="card">
          <div className="muted">Tokens</div>
          <strong style={{ fontSize: 18 }}>{run.totals.tokens.toLocaleString()}</strong>
        </div>
        <div className="card">
          <div className="muted">Cost</div>
          <strong style={{ fontSize: 18 }}>${run.totals.cost_usd.toFixed(5)}</strong>
        </div>
        <div className="card">
          <div className="muted">Wall clock</div>
          <strong style={{ fontSize: 18 }}>{run.totals.latency_s.toFixed(1)}s</strong>
        </div>
      </div>

      {run.terminal_reason && (
        <div className="banner banner-info">
          <span>{run.terminal_reason}</span>
        </div>
      )}

      <div className="card">
        <h3>Node by node</h3>
        {run.trace.length === 0 ? (
          <p className="muted">No trace was recorded for this run.</p>
        ) : (
          run.trace.map((entry, i) => (
            <div key={i}>
              <button
                className="node-row"
                style={{ width: '100%', textAlign: 'left', cursor: 'pointer' }}
                onClick={() => setOpen(open === i ? null : i)}
                aria-expanded={open === i}
                data-testid="trace-node"
              >
                <span
                  className={`node-dot ${entry.tool_calls?.some((c) => !c.ok) ? 'error' : ''}`}
                />
                <span className="node-name">{entry.node}</span>
                <span className="muted">{entry.seconds.toFixed(2)}s</span>
                {entry.llm_calls?.length > 0 && (
                  <span className="badge">{entry.llm_calls.length} LLM</span>
                )}
                {entry.tool_calls?.length > 0 && (
                  <span className="badge">{entry.tool_calls.length} tools</span>
                )}
              </button>
              {open === i && (
                <div className="card" style={{ marginLeft: 18 }}>
                  {entry.llm_calls?.length > 0 && (
                    <>
                      <h3>Model calls</h3>
                      <table>
                        <thead>
                          <tr>
                            <th>Model</th>
                            <th>In</th>
                            <th>Out</th>
                            <th>Latency</th>
                            <th>Cost</th>
                          </tr>
                        </thead>
                        <tbody>
                          {entry.llm_calls.map((call, j) => (
                            <tr key={j}>
                              <td>
                                <code>{call.model}</code>
                              </td>
                              <td>{call.prompt_tokens}</td>
                              <td>{call.completion_tokens}</td>
                              <td>{call.latency_s.toFixed(2)}s</td>
                              <td>${call.cost_usd.toFixed(6)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </>
                  )}
                  {entry.tool_calls?.length > 0 && (
                    <>
                      <h3 style={{ marginTop: 12 }}>Tool calls</h3>
                      {entry.tool_calls.map((call, j) => (
                        <div key={j} style={{ marginBottom: 10 }}>
                          <div className="row">
                            <span className={`badge ${call.ok ? 'badge-good' : 'badge-bad'}`}>
                              {call.ok ? 'ok' : 'failed'}
                            </span>
                            <code>
                              {call.server}.{call.tool}
                            </code>
                            <span className="faint">{call.latency_s.toFixed(2)}s</span>
                            <span className="faint">as {call.agent}</span>
                          </div>
                          <pre>{JSON.stringify(call.arguments, null, 2).slice(0, 500)}</pre>
                          {call.error && <div className="muted">{call.error}</div>}
                        </div>
                      ))}
                    </>
                  )}
                </div>
              )}
            </div>
          ))
        )}
      </div>

      {Object.keys(run.node_timings).length > 0 && (
        <div className="card">
          <h3>Per-node cost</h3>
          <table>
            <thead>
              <tr>
                <th>Node</th>
                <th>Seconds</th>
                <th>Tokens</th>
                <th>Cost</th>
                <th>Prompt version</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(run.node_timings).map(([node, t]) => (
                <tr key={node}>
                  <td>{node}</td>
                  <td>{typeof t.seconds === 'number' ? t.seconds.toFixed(3) : '—'}</td>
                  <td>{t.tokens ?? 0}</td>
                  <td>${(t.cost_usd ?? 0).toFixed(6)}</td>
                  <td>{t.prompt_version ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {Object.keys(run.retrieval).length > 0 && (
        <div className="card">
          <h3>Retrieval diagnostics</h3>
          <pre>{JSON.stringify(run.retrieval, null, 2)}</pre>
        </div>
      )}

      <Link className="btn" href="/runs">
        Back to all runs
      </Link>
    </>
  );
}
