import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/**
 * Register the model-free, nonce-bound capability probe used by Hephaestus.
 * The host separately issues RPC get_commands so command discovery remains a
 * native Pi response rather than extension-authored evidence.
 */
export default function hephaestusCapabilityProbe(pi: ExtensionAPI) {
  pi.registerCommand("hephaestus-preflight", {
    description: "Report active Pi tools for Hephaestus package preflight",
    handler: async (args, ctx) => {
      const nonce = args.trim();
      if (!/^[0-9a-f]{32}$/.test(nonce)) {
        ctx.ui.notify(JSON.stringify({ error: "invalid_nonce" }), "error");
        ctx.shutdown();
        return;
      }
      ctx.ui.notify(
        JSON.stringify({
          nonce,
          reported_commands: pi.getCommands(),
          active_tools: pi.getActiveTools(),
          all_tools: pi.getAllTools(),
        }),
        "info",
      );
      ctx.shutdown();
    },
  });
}
