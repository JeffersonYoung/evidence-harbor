import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';
import { registerTools } from './register.js';

export default definePluginEntry({
  id: 'evidenceharbor',
  name: 'EvidenceHarbor',
  description: 'Research tools backed by the same EvidenceHarbor MCP and authenticated API',
  register: registerTools
});
