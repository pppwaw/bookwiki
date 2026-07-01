import { RootProvider } from 'fumadocs-ui/provider/next';
import 'katex/dist/katex.css';
import './global.css';
import { Inter } from 'next/font/google';
import { AISearch, AISearchPanel, AISearchTrigger } from '@/components/ai/search';
import SearchDialog from '@/components/search-dialog';
import { MessageCircleIcon } from 'lucide-react';
import { cn } from '@/lib/cn';
import { buttonVariants } from 'fumadocs-ui/components/ui/button';

const inter = Inter({
  subsets: ['latin'],
  variable: '--font-inter',
  display: 'swap',
});

function visionEnabledFromEnv(): boolean {
  const raw = process.env.BOOKWIKI_CHAT_VISION?.trim().toLowerCase();
  return raw === '1' || raw === 'true' || raw === 'yes';
}

export default function Layout({ children }: LayoutProps<'/'>) {
  const visionEnabled = visionEnabledFromEnv();

  return (
    <html lang="en" className={inter.variable} suppressHydrationWarning>
      <body className="flex flex-col min-h-screen">
        <RootProvider search={{ SearchDialog }}>
          <AISearch visionEnabled={visionEnabled}>
            <AISearchPanel />
            <AISearchTrigger
              position="float"
              className={cn(
                buttonVariants({
                  variant: 'secondary',
                  className: 'text-fd-muted-foreground rounded-2xl',
                }),
              )}
            >
              <MessageCircleIcon className="size-4.5" />
              Ask AI
            </AISearchTrigger>
          </AISearch>
          {children}
        </RootProvider>
      </body>
    </html>
  );
}
