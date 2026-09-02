import type { Metadata } from 'next';
import './globals.css';
import './monitor-panels.css';
import './download-panel.css';
import './telegram-settings.css';
import './update-panel.css';
import { TooltipProvider } from '@/components/ui/tooltip';
const title = 'PULSE · 合约异动雷达';
const description =
  '币安 USDT 计价的全部 U 本位合约异常波动监测，涵盖加密资产、传统金融、永续及交割合约。四种形态、5 分钟与 15 分钟周期；支持 Telegram 中文纯文字信号和新合约提醒。';
export const metadata: Metadata = {
  title,
  description,
  metadataBase: new URL('http://127.0.0.1:5173'),
  icons: { icon: '/favicon.svg' },
  openGraph: {
    title,
    description,
    locale: 'zh_CN',
    type: 'website',
    images: [{ url: '/og.png', width: 1729, height: 910, alt: title }],
  },
  twitter: {
    card: 'summary_large_image',
    title,
    description,
    images: ['/og.png'],
  },
  robots: { index: false, follow: false },
};
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" className="dark">
      <body>
        <TooltipProvider delay={250}>{children}</TooltipProvider>
      </body>
    </html>
  );
}
