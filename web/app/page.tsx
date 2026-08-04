import Image from "next/image";
import Link from "next/link";
import { auth } from "../auth";

const TEASER_SQL = `SELECT player_name, shot_distance, action_type, game_date
FROM nba.shot_detail
WHERE shot_made_flag = 1
ORDER BY shot_distance DESC
LIMIT 20`;

// Real output of the query above against the loaded season (verified 2026-08-04).
const TEASER_ROWS = [
  ["Mikal Bridges", "62", "Pullup Jump shot", "2024-03-23"],
  ["Naji Marshall", "61", "Jump Bank Shot", "2023-11-14"],
  ["Max Strus", "58", "Pullup Jump shot", "2024-02-27"],
  ["Payton Pritchard", "50", "Pullup Jump shot", "2024-02-09"],
];

export default async function Home() {
  const session = await auth();

  return (
    <div className="flex flex-1 flex-col items-center px-4 py-14">
      <div className="w-full max-w-3xl">
        <h1 className="flex items-center gap-3 font-mono text-4xl font-semibold tracking-tight">
          <Image src="/logo.png" alt="" width={48} height={48} />
          bball-oracle
        </h1>
        <p className="mt-4 max-w-xl text-lg text-ink-muted">
          Write SQL against every play of the NBA season. A schema browser,
          autocomplete editor, and instant results over real play-by-play and
          shot-location data — no setup, just queries.
        </p>

        <div className="mt-6 flex flex-wrap items-center gap-x-6 gap-y-1 font-mono text-sm text-ink-faint">
          <span>
            <span className="text-ink">567,662</span> plays
          </span>
          <span>
            <span className="text-ink">218,701</span> shots
          </span>
          <span>2023-24 regular season</span>
        </div>

        <div className="mt-8 flex items-center gap-4">
          <Link
            href={session ? "/sandbox" : "/signin"}
            className="rounded-panel bg-accent px-5 py-2 text-sm font-semibold text-accent-ink hover:opacity-90"
          >
            {session ? "Open sandbox" : "Sign in with GitHub"}
          </Link>
          <span className="text-xs text-ink-faint">Free, read-only access.</span>
        </div>

        <div className="mt-14 overflow-hidden rounded-panel border border-edge">
          <div className="border-b border-edge bg-surface-raised px-4 py-2 font-mono text-xs text-ink-faint">
            Who made the deepest shots of the season?
          </div>
          <pre className="overflow-x-auto bg-surface-inset px-4 py-3 font-mono text-xs leading-relaxed text-ink-muted">
            {TEASER_SQL}
          </pre>
          <div className="overflow-x-auto border-t border-edge">
            <table className="w-full border-collapse font-mono text-xs">
              <thead>
                <tr className="bg-surface-raised text-left">
                  {["player_name", "shot_distance", "action_type", "game_date"].map(
                    (col) => (
                      <th
                        key={col}
                        className="border-b border-edge px-4 py-2 font-semibold text-ink"
                      >
                        {col}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody>
                {TEASER_ROWS.map((row) => (
                  <tr key={row.join()} className="border-b border-edge/50">
                    {row.map((cell, i) => (
                      <td key={i} className="whitespace-nowrap px-4 py-1.5 text-ink-muted">
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
                <tr>
                  <td
                    colSpan={4}
                    className="px-4 py-1.5 text-ink-faint"
                  >
                    … 16 more rows
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <dl className="mt-14 grid gap-8 sm:grid-cols-3">
          <div>
            <dt className="text-sm font-semibold">Event-grain data</dt>
            <dd className="mt-1 text-sm text-ink-muted">
              Every possession, substitution, and shot — including court
              coordinates and shot zones — not box-score summaries.
            </dd>
          </div>
          <div>
            <dt className="text-sm font-semibold">A real SQL editor</dt>
            <dd className="mt-1 text-sm text-ink-muted">
              CodeMirror with schema-aware autocomplete, example queries to
              start from, and your query history a click away.
            </dd>
          </div>
          <div>
            <dt className="text-sm font-semibold">Safe by construction</dt>
            <dd className="mt-1 text-sm text-ink-muted">
              Queries run through a SELECT-only Postgres role with a 5-second
              timeout and 1,000-row cap. Explore freely; nothing can break.
            </dd>
          </div>
        </dl>
      </div>
    </div>
  );
}
