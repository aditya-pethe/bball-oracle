import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    globalSetup: ["./lib/test-cluster.ts"],
    hookTimeout: 120_000,
    testTimeout: 30_000,
    // Suites share one cluster and write to the same app.query_log; the executor
    // suite counts log rows and temporarily revokes INSERT, so files must not race.
    fileParallelism: false,
  },
});
