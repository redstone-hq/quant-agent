import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output goes into src/api/server.py's /cockpit StaticFiles mount
// (src/api/static_cockpit/). That folder is gitignored build output: the
// deploy, the branch preview and CI each build it, passing --outDir to
// build into a temp folder that is swapped in only on success. See
// frontend/README.md.
export default defineConfig({
  plugins: [react()],
  base: "/cockpit/",
  build: {
    outDir: "../src/api/static_cockpit",
    emptyOutDir: true,
  },
  server: {
    // `server.*` only affects `vite dev` — never `vite build` — so this
    // block has zero effect on production, which serves the bundle built
    // at deploy (see the outDir comment above).
    //
    // Vite's DNS-rebinding protection rejects any request whose Host
    // header isn't localhost/an IP/an already-known pattern, which blocks
    // the canonical Tailscale MagicDNS FQDN (docs/STATE.md) even though
    // the dev server is already scoped to the Tailscale interface only
    // (see ops/preview/README.md for the equivalent IP-vs-MagicDNS note
    // on the branch_preview.py path, which doesn't have this check).
    allowedHosts: ["ovh-vps.wallaby-bowfin.ts.net"],
    // `npm run dev` proxies API calls to the live local Mission Control
    // API so the cockpit can be developed against real read-only data
    // without a build step.
    proxy: {
      "^/(account|positions|holdings|orders|trades|runs|decisions|agents|analysts|reflections|candidates|journal|research|search|health|prices|quotes|events|company)": {
        target: "http://127.0.0.1:8800",
        changeOrigin: true,
      },
    },
  },
});
