import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone server bundle so the Docker image ships node_modules-free.
  output: "standalone",
};

export default nextConfig;
