'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { api, setToken } from '@/lib/api/client';

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('reviewer@gigbid.app');
  const [password, setPassword] = useState('');
  const [persona, setPersona] = useState('p_ai_engineer');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const path = mode === 'login' ? '/auth/login' : '/auth/register';
      const body =
        mode === 'login'
          ? { email, password }
          : { email, password, persona_id: persona, display_name: email.split('@')[0] };
      const result = await api.post<{ access_token: string }>(path, body);
      setToken(result.access_token);
      router.push('/inbox');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign-in failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ maxWidth: 420, margin: '8vh auto' }}>
      <div className="brand" style={{ fontSize: 28, padding: '0 0 6px' }}>
        Gig<span>Bid</span>
      </div>
      <p className="muted" style={{ marginBottom: 20 }}>
        Finds work worth bidding on, drafts proposals grounded in your real portfolio, and never
        sends anything without you approving it.
      </p>

      <form className="card" onSubmit={submit}>
        <div className="field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            required
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        {mode === 'register' && (
          <div className="field">
            <label htmlFor="persona">Portfolio</label>
            <select id="persona" value={persona} onChange={(e) => setPersona(e.target.value)}>
              <option value="p_ai_engineer">Amara Osei — AI/ML engineer</option>
              <option value="p_fullstack_dev">Rohan Mehta — full-stack developer</option>
              <option value="p_data_analyst">Clara Bauer — data analyst</option>
            </select>
          </div>
        )}

        {error && (
          <div className="banner banner-bad" role="alert" data-testid="login-error">
            {error}
          </div>
        )}

        <button className="btn-primary" type="submit" disabled={busy} data-testid="submit-login">
          {busy ? 'Working…' : mode === 'login' ? 'Sign in' : 'Create account'}
        </button>

        <button
          type="button"
          className="btn-sm"
          style={{ marginTop: 10, width: '100%' }}
          onClick={() => setMode(mode === 'login' ? 'register' : 'login')}
        >
          {mode === 'login' ? 'Create an account instead' : 'I already have an account'}
        </button>
      </form>
    </div>
  );
}
