import { requireSession } from "../../../lib/require-session";
import { getSandboxPool } from "../../../lib/execute-query";

type ColumnRow = {
  table_name: string;
  column_name: string;
  data_type: string;
};

export async function GET() {
  const session = await requireSession();
  if (!session) {
    return Response.json({ error: "authentication required" }, { status: 401 });
  }

  const { rows } = await getSandboxPool().query<ColumnRow>(
    `SELECT table_name, column_name, data_type
       FROM information_schema.columns
      WHERE table_schema = 'nba'
      ORDER BY table_name, ordinal_position`,
  );

  const tables: { name: string; columns: { name: string; type: string }[] }[] = [];
  const byName = new Map<string, (typeof tables)[number]>();
  for (const row of rows) {
    let table = byName.get(row.table_name);
    if (!table) {
      table = { name: row.table_name, columns: [] };
      byName.set(row.table_name, table);
      tables.push(table);
    }
    table.columns.push({ name: row.column_name, type: row.data_type });
  }

  return Response.json({ tables });
}
