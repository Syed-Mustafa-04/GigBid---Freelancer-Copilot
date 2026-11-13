'use client';

import { useEffect, useState } from 'react';
import { api, getToken, type SystemStatus } from '@/lib/api/client';

/**
 * Degraded-mode banner.
 *
 * "No jobs found" and "the marketplace is down" must never look the same to
 * the user, so a marketplace outage is stated here rather than inferred from
 * an empty list. The offline-LLM warning is equally load-bearing: without it
 * a demo on the deterministic stub looks like a working system producing bad
 * proposals.
 */
export function StatusBanner() {
  const [status, setStatus] = useState<SystemStatus | null>(null);

  useEffect(() => {
    if (!getToken()) return;
    let cancelled = false;
    const check = async () => {
      try {
        const data = await api.get<SystemStatus>('/system/status');
        if (!cancelled) setStatus(data);
      } catch {
        if (!cancelled) setStatus(null);
      }
    };
    void check();
    const timer = setInterval(check, 30_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  if (!status) return null;

  return (
    <>
      {!status.marketplace.available && (
        <div className="banner banner-bad" role="status" data-testid="banner-marketplace-down">
          <strong>Marketplace unavailable.</strong>
          <span>
            GigBid cannot reach {status.marketplace.adapter}. Job lists may be stale and nothing
            can be submitted. Queued work resumes automatically when it returns.
          </span>
        </div>
      )}
      {status.llm.offline_stub && (
        <div className="banner banner-warn" role="status" data-testid="banner-offline-llm">
          <strong>Offline model.</strong>
          <span>
            No LLM API key is configured, so GigBid is running its deterministic stub. The graph
            executes end to end, but the proposal text and the scores are placeholders, not model
            output. Add <code>OPEN_LLM_API_KEY</code> to get real results.
          </span>
        </div>
      )}
    </>
  );
}
