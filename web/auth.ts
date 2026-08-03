import NextAuth from "next-auth";
import GitHub from "next-auth/providers/github";
import PostgresAdapter from "@auth/pg-adapter";
import { Pool } from "pg";

// max 5: app_rw has CONNECTION LIMIT 10 and shares it with the query-logging pool.
const pool = new Pool({
  connectionString: process.env.APP_RW_DATABASE_URL,
  max: 5,
});

export const { handlers, auth, signIn, signOut } = NextAuth({
  adapter: PostgresAdapter(pool),
  providers: [GitHub],
  // Database sessions, not JWT: this product is abuse-prone (users run arbitrary SQL),
  // so a session must be revocable server-side by deleting its app.sessions row. A JWT
  // stays valid until it expires no matter what we do.
  session: { strategy: "database" },
  callbacks: {
    session({ session, user }) {
      // user is the raw app.users row, so user.id is a number (pg parses int4 as a
      // number) despite Auth.js typing it as string. Stringify to match the declared
      // type; requireSession() is the one place that converts it back to a number.
      if (session.user) session.user.id = String(user.id);
      return session;
    },
  },
});
