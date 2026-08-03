import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    globalSetup: ["./lib/test-cluster.ts"],
    hookTimeout: 120_000,
    testTimeout: 30_000,
  },
});
