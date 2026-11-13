'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';
import { api, clearToken, getToken } from '@/lib/api/client';

const LINKS = [
  { href: '/inbox', label: 'Job inbox' },
  { href: '/pipeline', label: 'Pipeline' },
  { href: '/messages', label: 'Messages' },
  { href: '/runs', label: 'Agent runs' },
  { href: '/analytics', label: 'Analytics' },
  { href: '/settings', label: 'Settings' },
];

export function Nav() {
  const pathname = usePathname();
  const [pending, setPending] = useState(0);
  const [signedIn, setSignedIn] = useState(false);

  useEffect(() => {
    setSignedIn(!!getToken());
  }, [pathname]);

  useEffect(() => {
    if (!signedIn) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const data = await api.get<{ approvals: unknown[] }>('/approvals');
        if (!cancelled) setPending(data.approvals.length);
      } catch {
        /* the badge is not worth an error state */
      }
    };
    void poll();
    const timer = setInterval(poll, 15_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [signedIn, pathname]);

  if (pathname === '/login') return null;

  return (
    <nav className="sidebar" aria-label="Main">
      <div className="brand">
        Gig<span>Bid</span>
      </div>
      {LINKS.map((link) => (
        <Link
          key={link.href}
          href={link.href}
          className="nav-link"
          aria-current={pathname.startsWith(link.href) ? 'page' : undefined}
        >
          <span>{link.label}</span>
          {link.href === '/pipeline' && pending > 0 && (
            <span className="badge badge-accent" aria-label={`${pending} awaiting approval`}>
              {pending}
            </span>
          )}
        </Link>
      ))}
      <div style={{ marginTop: 'auto', paddingTop: 16 }}>
        <button
          className="btn-sm"
          style={{ width: '100%' }}
          onClick={() => {
            clearToken();
            window.location.href = '/login';
          }}
        >
          Sign out
        </button>
      </div>
    </nav>
  );
}
