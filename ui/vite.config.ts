import { defineConfig } from "vite";
import { thirdPartyNotices } from "./scripts/licenses.mjs";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";
export default defineConfig({
  plugins: [react(), tailwindcss(), thirdPartyNotices()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  base: "./",
  build: {
    outDir: "dist",
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (
            id.includes("node_modules/react-markdown") ||
            id.includes("node_modules/remark-") ||
            id.includes("node_modules/micromark") ||
            id.includes("node_modules/mdast-") ||
            id.includes("node_modules/hast-")
          )
            return "markdown";
        },
      },
    },
  },
});
