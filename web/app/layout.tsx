import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Image from "next/image";
import Link from "next/link";
import { auth, signOut } from "../auth";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "bball-oracle — SQL sandbox for NBA play-by-play data",
  description:
    "Write read-only SQL against every play and shot of the 2023-24 NBA season: schema browser, CodeMirror editor, instant results.",
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const session = await auth();
  const user = session?.user;

  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body className="flex min-h-screen flex-col">
        <header className="flex items-center justify-between border-b border-edge px-4 py-2">
          <Link
            href="/"
            className="flex items-center gap-2 font-mono text-sm font-semibold text-ink"
          >
            <Image src="/logo.png" alt="" width={22} height={22} />
            bball-oracle
          </Link>
          {user && (
            <div className="flex items-center gap-3">
              <span className="flex items-center gap-2 text-sm text-ink-muted">
                {user.image && (
                  /* GitHub-CDN avatar; next/image would need remotePatterns config for one 24px img */
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={user.image}
                    alt=""
                    className="h-6 w-6 rounded-full"
                  />
                )}
                {user.name ?? user.email}
              </span>
              <form
                action={async () => {
                  "use server";
                  await signOut({ redirectTo: "/" });
                }}
              >
                <button
                  type="submit"
                  className="rounded-panel border border-edge px-2 py-1 text-xs text-ink-muted hover:bg-surface-raised hover:text-ink"
                >
                  Sign out
                </button>
              </form>
            </div>
          )}
        </header>
        <main className="flex min-h-0 flex-1 flex-col">{children}</main>
        <footer className="border-t border-edge px-4 py-2 text-center text-[11px] leading-relaxed text-ink-faint">
          NBA play-by-play data via the{" "}
          <a
            href="https://github.com/shufinskiy/nba_data"
            className="underline decoration-edge underline-offset-2 hover:text-ink-muted"
          >
            nba_data
          </a>{" "}
          project, sourced from stats.nba.com. Not affiliated with or endorsed
          by the NBA; underlying data is governed by NBA.com&apos;s terms of use.
        </footer>
      </body>
    </html>
  );
}
