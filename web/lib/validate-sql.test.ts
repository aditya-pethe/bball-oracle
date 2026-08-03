import { describe, expect, it } from "vitest";
import { validateSql } from "./validate-sql";

async function expectOk(sql: string) {
  const result = await validateSql(sql);
  expect(result).toEqual({ ok: true });
}

async function expectRejected(sql: string, reasonSubstring?: string) {
  const result = await validateSql(sql);
  expect(result.ok).toBe(false);
  if (!result.ok && reasonSubstring) {
    expect(result.reason).toContain(reasonSubstring);
  }
}

describe("validateSql — accepts read-only SELECTs", () => {
  it("accepts a plain SELECT", async () => {
    await expectOk("SELECT 1");
  });

  it("accepts SELECT * FROM a table", async () => {
    await expectOk("SELECT * FROM nba.pbp_event");
  });

  it("accepts joins", async () => {
    await expectOk(
      "SELECT p.event_id, s.zone FROM nba.pbp_event p JOIN nba.shot_detail s ON p.event_id = s.event_id"
    );
  });

  it("accepts aggregates and GROUP BY", async () => {
    await expectOk(
      "SELECT team_id, COUNT(*) FROM nba.pbp_event GROUP BY team_id HAVING COUNT(*) > 10"
    );
  });

  it("accepts subqueries in FROM", async () => {
    await expectOk("SELECT * FROM (SELECT 1 AS x) sub");
  });

  it("accepts scalar subqueries", async () => {
    await expectOk("SELECT (SELECT 1) AS x");
  });

  it("accepts sublinks in WHERE", async () => {
    await expectOk("SELECT * FROM nba.pbp_event WHERE team_id IN (SELECT team_id FROM nba.pbp_event)");
  });

  it("accepts a read-only CTE", async () => {
    await expectOk("WITH x AS (SELECT 1 AS n) SELECT * FROM x");
  });

  it("accepts WITH RECURSIVE over a read-only CTE", async () => {
    await expectOk(
      "WITH RECURSIVE x AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM x WHERE n < 5) SELECT * FROM x"
    );
  });

  it("accepts set operations (UNION)", async () => {
    await expectOk("SELECT 1 UNION SELECT 2");
  });

  it("accepts UNION ALL / INTERSECT / EXCEPT", async () => {
    await expectOk("SELECT 1 UNION ALL SELECT 2");
    await expectOk("SELECT 1 INTERSECT SELECT 1");
    await expectOk("SELECT 1 EXCEPT SELECT 2");
  });

  it("accepts window functions", async () => {
    await expectOk(
      "SELECT event_id, ROW_NUMBER() OVER (PARTITION BY team_id ORDER BY event_id) FROM nba.pbp_event"
    );
  });

  it("accepts comments around a valid SELECT", async () => {
    await expectOk("-- leading comment\nSELECT 1 /* inline */ AS x -- trailing comment");
  });

  it("accepts weird casing and whitespace", async () => {
    await expectOk("   SeLeCt   1   ");
    await expectOk("select\n\t1\n");
  });

  it("accepts a trailing semicolon with only a comment after it", async () => {
    await expectOk("SELECT 1;\n-- trailing comment");
  });

  it("accepts a trailing semicolon alone", async () => {
    await expectOk("SELECT 1;");
  });

  it("accepts semicolons inside string literals without treating it as multi-statement", async () => {
    await expectOk("SELECT ';' AS x");
    await expectOk("SELECT 'a; DROP TABLE nba.pbp_event; b' AS x");
  });

  it("accepts dollar-quoted strings containing semicolons and SQL keywords", async () => {
    await expectOk("SELECT $$hi; DELETE FROM t; bye$$ AS x");
    await expectOk("SELECT $tag$another; DROP TABLE t;$tag$ AS x");
  });

  it("ignores unicode content inside a string literal (parser sees real tokens, not homoglyphs)", async () => {
    const fullwidthDropTable = String.fromCharCode(
      0xff24, 0xff32, 0xff2f, 0xff30, 0x20, 0xff34, 0xff21, 0xff22, 0xff2c, 0xff25
    );
    await expectOk(`SELECT '${fullwidthDropTable}' AS x`);
  });
});

describe("validateSql — rejects mutations and DDL", () => {
  it("rejects DELETE", async () => {
    await expectRejected("DELETE FROM nba.pbp_event", "DeleteStmt");
  });

  it("rejects UPDATE", async () => {
    await expectRejected("UPDATE nba.pbp_event SET team_id = 1", "UpdateStmt");
  });

  it("rejects INSERT", async () => {
    await expectRejected("INSERT INTO nba.pbp_event (event_id) VALUES (1)", "InsertStmt");
  });

  it("rejects TRUNCATE", async () => {
    await expectRejected("TRUNCATE nba.pbp_event", "TruncateStmt");
  });

  it("rejects DROP TABLE", async () => {
    await expectRejected("DROP TABLE nba.pbp_event", "DropStmt");
  });

  it("rejects CREATE TABLE", async () => {
    await expectRejected("CREATE TABLE foo (id int)", "CreateStmt");
  });

  it("rejects ALTER TABLE", async () => {
    await expectRejected("ALTER TABLE nba.pbp_event ADD COLUMN foo int", "AlterTableStmt");
  });

  it("rejects GRANT", async () => {
    await expectRejected("GRANT SELECT ON nba.pbp_event TO PUBLIC", "GrantStmt");
  });

  it("rejects COPY", async () => {
    await expectRejected("COPY nba.pbp_event TO STDOUT", "CopyStmt");
  });

  it("rejects VACUUM", async () => {
    await expectRejected("VACUUM nba.pbp_event", "VacuumStmt");
  });

  it("rejects CREATE MATERIALIZED VIEW ... AS SELECT", async () => {
    await expectRejected("CREATE MATERIALIZED VIEW mv AS SELECT 1", "CreateTableAsStmt");
  });

  it("rejects transaction control statements", async () => {
    await expectRejected("BEGIN", "TransactionStmt");
    await expectRejected("COMMIT", "TransactionStmt");
  });
});

describe("validateSql — rejects statement shapes that aren't a plain single SELECT", () => {
  it("rejects EXPLAIN", async () => {
    await expectRejected("EXPLAIN SELECT 1", "ExplainStmt");
  });

  it("rejects SELECT INTO", async () => {
    await expectRejected("SELECT 1 INTO TEMP foo", "INTO");
  });

  it("rejects SELECT ... FOR UPDATE", async () => {
    await expectRejected("SELECT * FROM nba.pbp_event FOR UPDATE");
  });

  it("rejects SELECT ... FOR SHARE", async () => {
    await expectRejected("SELECT * FROM nba.pbp_event FOR SHARE");
  });

  it("rejects SELECT ... FOR NO KEY UPDATE", async () => {
    await expectRejected("SELECT * FROM nba.pbp_event FOR NO KEY UPDATE");
  });

  it("rejects SELECT ... FOR KEY SHARE", async () => {
    await expectRejected("SELECT * FROM nba.pbp_event FOR KEY SHARE");
  });

  it("rejects a locking clause nested inside a CTE", async () => {
    await expectRejected("WITH x AS (SELECT * FROM nba.pbp_event FOR UPDATE) SELECT * FROM x");
  });
});

describe("validateSql — rejects multiple statements", () => {
  it("rejects two SELECTs separated by a semicolon", async () => {
    await expectRejected("SELECT 1; SELECT 2", "single SELECT");
  });

  it("rejects a SELECT followed by a DELETE", async () => {
    await expectRejected("SELECT 1; DELETE FROM nba.pbp_event", "single SELECT");
  });

  it("rejects the classic comment-smuggling injection shape", async () => {
    await expectRejected("SELECT 1; -- \nDELETE FROM nba.pbp_event", "single SELECT");
  });
});

describe("validateSql — rejects CTEs containing anything other than plain SELECTs", () => {
  it("rejects a DELETE...RETURNING CTE feeding a SELECT", async () => {
    await expectRejected(
      "WITH x AS (DELETE FROM nba.pbp_event RETURNING *) SELECT * FROM x",
      "DeleteStmt"
    );
  });

  it("rejects an UPDATE...RETURNING CTE feeding a SELECT", async () => {
    await expectRejected(
      "WITH x AS (UPDATE nba.pbp_event SET team_id = 1 RETURNING *) SELECT * FROM x",
      "UpdateStmt"
    );
  });

  it("rejects an INSERT...RETURNING CTE feeding a SELECT", async () => {
    await expectRejected(
      "WITH x AS (INSERT INTO nba.pbp_event (event_id) VALUES (1) RETURNING *) SELECT * FROM x",
      "InsertStmt"
    );
  });

  it("rejects a mutation nested two CTE levels deep", async () => {
    await expectRejected(
      "WITH a AS (WITH b AS (DELETE FROM nba.pbp_event RETURNING *) SELECT * FROM b) SELECT * FROM a",
      "DeleteStmt"
    );
  });

  it("rejects a mutating CTE inside a subquery in FROM", async () => {
    await expectRejected(
      "SELECT * FROM (WITH x AS (DELETE FROM nba.pbp_event RETURNING *) SELECT * FROM x) sub",
      "DeleteStmt"
    );
  });

  it("rejects a mutating CTE inside a sublink in WHERE", async () => {
    await expectRejected(
      "SELECT * FROM nba.pbp_event WHERE event_id IN (WITH y AS (DELETE FROM nba.shot_detail RETURNING event_id) SELECT event_id FROM y)",
      "DeleteStmt"
    );
  });

  it("rejects a mutating CTE inside one arm of a UNION", async () => {
    await expectRejected(
      "(WITH x AS (DELETE FROM nba.pbp_event RETURNING *) SELECT * FROM x) UNION SELECT 1",
      "DeleteStmt"
    );
  });

  it("rejects a mutating CTE inside a LATERAL join", async () => {
    await expectRejected(
      "SELECT * FROM nba.pbp_event p CROSS JOIN LATERAL (WITH x AS (DELETE FROM nba.shot_detail RETURNING *) SELECT * FROM x) sub",
      "DeleteStmt"
    );
  });
});

describe("validateSql — VALUES lists", () => {
  it("treats a bare VALUES list as a read-only SelectStmt and accepts it", async () => {
    await expectOk("VALUES (1), (2)");
  });
});

describe("validateSql — fails closed on bad input", () => {
  it("rejects empty input", async () => {
    await expectRejected("");
  });

  it("rejects whitespace-only input", async () => {
    await expectRejected("   \n\t  ");
  });

  it("rejects unparseable garbage", async () => {
    await expectRejected("this is not sql at all ((((");
  });

  it("rejects a syntactically broken SELECT", async () => {
    await expectRejected("SELECT FROM WHERE");
  });

  it("rejects input longer than 20000 characters", async () => {
    const longSql = "SELECT 1 -- " + "a".repeat(20_000);
    await expectRejected(longSql);
  });

  it("accepts input right at the 20000 character boundary", async () => {
    const padding = "-- " + "a".repeat(20_000 - "SELECT 1 ".length - 3);
    const sql = "SELECT 1 " + padding;
    expect(sql.length).toBeLessThanOrEqual(20_000);
    await expectOk(sql);
  });
});
