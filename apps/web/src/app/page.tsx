'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { getToken } from '@/lib/api/client';

export default function Home() {
  const router = useRouter();
  useEffect(() => {
    router.replace(getToken() ? '/inbox' : '/login');
  }, [router]);
  return (
    <div className="empty">
      <h3>GigBid</h3>
      <p>Loading…</p>
    </div>
  );
}
