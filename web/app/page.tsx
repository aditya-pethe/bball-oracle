import Link from "next/link";
import { auth } from "../auth";

export default async function Home() {
  const session = await auth();

  return (
    <div className="flex flex-1 items-center justify-center px-4">
      <div className="max-w-xl text-center">
        <h1 className="font-mono text-2xl font-semibold">bball-oracle</h1>
        <p className="mt-3 text-ink-muted">
          A SQL sandbox over real NBA play-by-play data: sign in, write
          read-only queries against every event and shot of the 2023-24 season,
          and see the results instantly.
        </p>
        <Link
          href={session ? "/sandbox" : "/signin"}
          className="mt-6 inline-block rounded-panel bg-accent px-5 py-2 text-sm font-semibold text-accent-ink hover:opacity-90"
        >
          {session ? "Open sandbox" : "Sign in"}
        </Link>
      </div>
    </div>
  );
}
