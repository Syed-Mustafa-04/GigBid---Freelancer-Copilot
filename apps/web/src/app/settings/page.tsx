'use client';

import { useCallback, useEffect, useState } from 'react';
import { api } from '@/lib/api/client';

interface MemoryRule {
  rule_id: string;
  store: string;
  text: string;
  origin: string;
  occurrences: number;
  confidence: number;
  categories: string[];
}

interface Settings {
  user: {
    email: string;
    persona_id: string;
    autonomy_level: string;
    hourly_rate_usd: number;
    min_project_usd: number;
    weekly_connects_budget: number;
    marketplace_mode: string;
  };
  saved_searches: {
    id: string;
    name: string;
    query: string;
    skills: string[];
    interval_seconds: number;
    enabled: boolean;
    last_run_at: string | null;
  }[];
  budgets: Record<string, number>;
  thresholds: Record<string, unknown>;
  rag_variant: string;
  model_profile: string;
  marketplace_mode: string;
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [memory, setMemory] = useState<{
    rules: Record<string, MemoryRule[]>;
    stats: Record<string, unknown>;
  } | null>(null);
  const [upwork, setUpwork] = useState<{ connected: boolean; encryption_available: boolean } | null>(
    null,
  );
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [s, m] = await Promise.all([
      api.get<Settings>('/settings'),
      api.get<{ rules: Record<string, MemoryRule[]>; stats: Record<string, unknown> }>(
        '/settings/memory',
      ),
    ]);
    setSettings(s);
    setMemory(m);
    try {
      setUpwork(await api.get('/auth/upwork/status'));
    } catch {
      setUpwork(null);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function patch(body: Record<string, unknown>) {
    await api.patch('/settings', body);
    setNote('Saved.');
    await load();
  }

  async function forget(ruleId: string) {
    await api.delete(`/settings/memory/${ruleId}`);
    setNote('Deleted. GigBid will stop applying that rule.');
    await load();
  }

  if (!settings)
    return (
      <div className="card">
        <div className="skeleton" style={{ width: '50%' }} />
      </div>
    );

  const styleRules = memory?.rules.style ?? [];
  const outcomeRules = memory?.rules.outcome ?? [];

  return (
    <>
      <div className="page-head">
        <h1>Settings</h1>
        <p>Rates, autonomy, connected accounts, and everything GigBid has learned about you.</p>
      </div>

      {note && <div className="banner banner-info">{note}</div>}

      <div className="grid grid-2">
        <div className="card">
          <h3>Your rates</h3>
          <div className="field">
            <label htmlFor="rate">Hourly rate (USD)</label>
            <input
              id="rate"
              type="number"
              defaultValue={settings.user.hourly_rate_usd}
              onBlur={(e) => patch({ hourly_rate_usd: Number(e.target.value) })}
            />
          </div>
          <div className="field">
            <label htmlFor="min">Minimum project (USD)</label>
            <input
              id="min"
              type="number"
              defaultValue={settings.user.min_project_usd}
              onBlur={(e) => patch({ min_project_usd: Number(e.target.value) })}
            />
          </div>
          <div className="field">
            <label htmlFor="connects">Weekly Connects budget</label>
            <input
              id="connects"
              type="number"
              defaultValue={settings.user.weekly_connects_budget}
              onBlur={(e) => patch({ weekly_connects_budget: Number(e.target.value) })}
            />
          </div>
        </div>

        <div className="card">
          <h3>Autonomy</h3>
          <div className="field">
            <label htmlFor="autonomy">How much GigBid may do on its own</label>
            <select
              id="autonomy"
              defaultValue={settings.user.autonomy_level}
              onChange={(e) => patch({ autonomy_level: e.target.value })}
              data-testid="autonomy-select"
            >
              <option value="manual">Manual — analyse only when I ask</option>
              <option value="assisted">Assisted — analyse and draft, I approve everything</option>
              <option value="auto_skip">
                Auto-skip — also discard clearly unsuitable jobs without asking
              </option>
            </select>
          </div>
          <p className="muted">
            No level sends anything. A proposal or message always needs your approval, and the
            marketplace tools refuse a write without an approval token.
          </p>

          <div className="field" style={{ marginTop: 14 }}>
            <label htmlFor="marketplace">Marketplace</label>
            <select
              id="marketplace"
              defaultValue={settings.user.marketplace_mode}
              onChange={(e) => patch({ marketplace_mode: e.target.value })}
            >
              <option value="gigsim">GigSim (simulator)</option>
              <option value="upwork_live">Upwork (live, via the official MCP server)</option>
            </select>
          </div>
          {upwork && (
            <div className="muted">
              Upwork account: {upwork.connected ? 'connected' : 'not connected'}
              {!upwork.encryption_available && (
                <div className="banner banner-warn" style={{ marginTop: 8 }}>
                  <span>
                    <code>TOKEN_ENCRYPTION_KEY</code> is not set, so OAuth tokens cannot be stored
                    encrypted. Live mode is disabled rather than storing a token in plaintext.
                  </span>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* ------------------------------------------------------- memory */}
      <div className="card" data-testid="memory-viewer">
        <div className="card-head">
          <div>
            <h3>What GigBid has learned</h3>
            <p className="muted">
              Nothing reaches this list automatically. A style rule needs the same edit to recur
              across separate proposals; an outcome rule needs enough observed results. You can
              delete anything here.
            </p>
          </div>
        </div>

        <h3 style={{ marginTop: 12 }}>Style ({styleRules.length})</h3>
        {styleRules.length === 0 ? (
          <p className="muted">
            Nothing yet. Edit a draft in the studio a couple of times and recurring patterns appear
            here.
          </p>
        ) : (
          styleRules.map((rule) => (
            <div className="spread" key={rule.rule_id} style={{ marginBottom: 8 }}>
              <div>
                <div>{rule.text}</div>
                <div className="faint">
                  from {rule.origin.replace('_', ' ')} · seen {rule.occurrences}× · confidence{' '}
                  {(rule.confidence * 100).toFixed(0)}%
                </div>
              </div>
              <button
                className="btn-sm btn-danger"
                onClick={() => forget(rule.rule_id)}
                data-testid="forget-rule"
              >
                Forget
              </button>
            </div>
          ))
        )}

        <h3 style={{ marginTop: 16 }}>Outcomes ({outcomeRules.length})</h3>
        {outcomeRules.length === 0 ? (
          <p className="muted">No category has enough observed outcomes yet.</p>
        ) : (
          outcomeRules.map((rule) => (
            <div className="spread" key={rule.rule_id} style={{ marginBottom: 8 }}>
              <div>
                <div>{rule.text}</div>
                <div className="faint">{rule.categories.join(', ')}</div>
              </div>
              <button className="btn-sm btn-danger" onClick={() => forget(rule.rule_id)}>
                Forget
              </button>
            </div>
          ))
        )}

        {memory && (
          <details style={{ marginTop: 12 }}>
            <summary className="muted" style={{ cursor: 'pointer' }}>
              What is never remembered
            </summary>
            <ul className="muted">
              {((memory.stats.never_promote as string[]) ?? []).map((item) => (
                <li key={item}>{item.replace(/_/g, ' ')}</li>
              ))}
            </ul>
          </details>
        )}
      </div>

      {/* ------------------------------------------------ saved searches */}
      <div className="card">
        <h3>Saved searches</h3>
        {settings.saved_searches.length === 0 ? (
          <p className="muted">None yet.</p>
        ) : (
          settings.saved_searches.map((s) => (
            <div className="spread" key={s.id} style={{ marginBottom: 8 }}>
              <div>
                <strong>{s.name}</strong>
                <div className="faint">
                  “{s.query}” · every {Math.round(s.interval_seconds / 60)} min ·{' '}
                  {s.last_run_at ? `last ran ${new Date(s.last_run_at).toLocaleString()}` : 'never run'}
                </div>
              </div>
              <button
                className="btn-sm btn-danger"
                onClick={async () => {
                  await api.delete(`/settings/searches/${s.id}`);
                  await load();
                }}
              >
                Remove
              </button>
            </div>
          ))
        )}
      </div>

      <div className="card">
        <h3>System</h3>
        <table>
          <tbody>
            <tr>
              <th>Model profile</th>
              <td>
                <code>{settings.model_profile}</code>
              </td>
            </tr>
            <tr>
              <th>Shipped RAG variant</th>
              <td>
                <code>{settings.rag_variant}</code>
              </td>
            </tr>
            <tr>
              <th>Marketplace</th>
              <td>
                <code>{settings.marketplace_mode}</code>
              </td>
            </tr>
            <tr>
              <th>Token budget per job</th>
              <td>{settings.budgets.max_tokens_per_job?.toLocaleString()}</td>
            </tr>
            <tr>
              <th>Max critic revisions</th>
              <td>{settings.budgets.max_revisions}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </>
  );
}
