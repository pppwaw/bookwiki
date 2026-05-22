import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "BookWiki",
  description: "Local BookWiki demo site",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const siteLanguage = process.env.BOOKWIKI_SITE_LANGUAGE || "zh-CN";

  return (
    <html lang={siteLanguage}>
      <body>{children}</body>
    </html>
  );
}
