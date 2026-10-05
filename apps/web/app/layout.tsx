import type { Metadata } from 'next';
import './globals.css';
export const metadata: Metadata = {
  title: '证据港 · EvidenceHarbor',
  description: '从可信资料，到有据可循的研究。你的统一研究工作空间。',
  robots: { index: false, follow: false },
};
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
