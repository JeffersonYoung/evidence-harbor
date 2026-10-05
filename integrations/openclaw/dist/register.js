import { readFileSync } from 'node:fs';
import { callMcp } from './bridge.js';
const schemas = JSON.parse(readFileSync(new URL('../tool-schemas.json', import.meta.url), 'utf8'));

export function registerTools(api, invoke = callMcp) {
  const editorEnabled = ['1', 'true', 'yes'].includes((process.env.EVIDENCEHARBOR_ENABLE_EDITOR_TOOLS || '').trim().toLowerCase());
  const editorSchemas = editorEnabled ? JSON.parse(readFileSync(new URL('../editor-tool-schemas.json', import.meta.url), 'utf8')) : [];
  for (const tool of [...schemas, ...editorSchemas]) {
    api.registerTool({
      name: `evidenceharbor_${tool.name}`,
      description: tool.description,
      parameters: tool.inputSchema,
      async execute(_id, params, signal) {
        const result = await invoke(tool.name, params, { signal });
        if (result.isError) throw new Error('EvidenceHarbor rejected the request; verify arguments and project permissions');
        return { content: result.content, details: result.structuredContent ?? {} };
      }
    }, { optional: true });
  }
}
