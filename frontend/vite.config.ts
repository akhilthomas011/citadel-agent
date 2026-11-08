import { defineConfig } from "vitest/config";

export default defineConfig({
  build: {
    outDir: "../src/citadel_agent/portal/static",
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    restoreMocks: true,
  },
});
