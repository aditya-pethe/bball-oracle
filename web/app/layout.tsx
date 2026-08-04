import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
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
  title: "bball-oracle",
  description: "SQL sandbox over NBA play-by-play data",
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
          <Link href="/" className="font-mono text-sm font-semibold text-ink">
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
      </body>
    </html>
  );
}
