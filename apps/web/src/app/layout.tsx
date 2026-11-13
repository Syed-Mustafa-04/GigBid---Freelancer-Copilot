import type { Metadata } from 'next';
import './globals.css';
import { Nav } from '@/components/Nav';
import { StatusBanner } from '@/components/StatusBanner';

export const metadata: Metadata = {
  title: 'GigBid',
  description:
    'A multi-agent copilot that finds freelance work worth bidding on, drafts proposals grounded in real portfolio evidence, and never sends anything without your approval.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <a href="#main" className="sr-only">
          Skip to main content
        </a>
        <div className="shell">
          <Nav />
          <main className="main" id="main">
            <StatusBanner />
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}
