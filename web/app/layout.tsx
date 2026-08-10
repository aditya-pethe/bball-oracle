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
      {/* `h-dvh`, not `min-h-dvh`, and the distinction is load-bearing.

          `min-h-dvh` sets a floor, not a ceiling, so the body is free to grow past the
          viewport. `flex-1` on <main> only distributes *free* space — it does not cap a
          child whose content is intrinsically taller — so a tall sandbox pushed the body
          down and the whole page scrolled, putting the agent composer below the fold no
          matter how correct the `min-h-0` chain beneath it was. `h-dvh` fixes the body to
          exactly the viewport, which is what forces <main> to shrink and hands scrolling
          to the panes that opt into it.

          `dvh` rather than `vh` because `100vh` on mobile is the height *without* browser
          chrome — the other way a pinned composer ends up just off-screen.

          Consequence: <main> is now the scroll container, so header and footer stay put on
          long pages (the landing page) instead of scrolling away. Pages that pin their own
          layout — the sandbox — set `overflow-hidden` on themselves and scroll internally. */}
      <body className="flex h-dvh flex-col overflow-hidden">
        <header className="flex items-center justify-between border-b border-edge px-4 py-2">
          <Link
            href="/"
            className="flex items-center gap-2 font-mono text-sm font-semibold text-ink"
          >
            <Image src="/logo.png" alt="" width={30} height={30} />
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
        {/* `overflow-y-auto` is the other half of the `h-dvh` change above: with the body
            fixed to the viewport, long pages need somewhere to scroll. The sandbox opts out
            by setting `overflow-hidden` on its own root and scrolling its panes instead. */}
        <main className="flex min-h-0 flex-1 flex-col overflow-y-auto">{children}</main>
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
