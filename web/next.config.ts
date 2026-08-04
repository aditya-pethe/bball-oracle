import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Bundling libpg-query rewrites the path to its .wasm file, which then fails to
  // load at runtime; keep it external so it resolves from node_modules.
  serverExternalPackages: ["libpg-query"],
};

export default nextConfig;
