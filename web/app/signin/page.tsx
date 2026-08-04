import { redirect } from "next/navigation";
import { auth, signIn } from "../../auth";

export default async function SignInPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const session = await auth();
  if (session) redirect("/sandbox");

  const { error } = await searchParams;

  return (
    <div className="flex flex-1 items-center justify-center">
      <div className="w-full max-w-sm rounded-panel border border-edge bg-surface-raised p-8">
        <h1 className="font-mono text-lg font-semibold">bball-oracle</h1>
        <p className="mt-2 text-sm text-ink-muted">
          Run read-only SQL against NBA play-by-play data. Sign in to open the
          sandbox.
        </p>
        {error && (
          <p className="mt-4 rounded-panel border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            Sign-in failed ({error}). Try again.
          </p>
        )}
        <form
          action={async () => {
            "use server";
            await signIn("github", { redirectTo: "/sandbox" });
          }}
        >
          <button
            type="submit"
            className="mt-6 w-full rounded-panel bg-accent px-4 py-2 text-sm font-semibold text-accent-ink hover:opacity-90"
          >
            Continue with GitHub
          </button>
        </form>
      </div>
    </div>
  );
}
