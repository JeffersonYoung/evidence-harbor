import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export async function callMcp(name, args, { python, timeoutMs = 65000, signal } = {}) {
  const interpreter = python || process.env.EVIDENCEHARBOR_PYTHON;
  if (!interpreter) throw new Error('Set EVIDENCEHARBOR_PYTHON to the absolute path of the installed Python interpreter');
  if (!process.env.EVIDENCEHARBOR_API_TOKEN) throw new Error('EVIDENCEHARBOR_API_TOKEN is required');
  const payload = JSON.stringify({ name, arguments: args });
  if (Buffer.byteLength(payload) > 1048576) throw new Error('MCP request exceeds 1 MiB');
  return new Promise((resolve, reject) => {
    const child = spawn(interpreter, [fileURLToPath(new URL('../bridge.py', import.meta.url))], {
      shell: false, stdio: ['pipe', 'pipe', 'ignore'], signal,
      env: Object.fromEntries(Object.entries(process.env).filter(([key]) => [
        'PATH', 'HOME', 'SYSTEMROOT', 'TEMP', 'TMP', 'LANG',
        'EVIDENCEHARBOR_API_URL', 'EVIDENCEHARBOR_API_TOKEN'
      ].includes(key)))
    });
    let output = ''; let exceeded = false; let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); }, timeoutMs);
    child.stdout.on('data', (data) => {
      output += data;
      if (Buffer.byteLength(output) > 8 * 1048576) { exceeded = true; child.kill('SIGKILL'); }
    });
    child.stdin.on('error', () => {});
    child.on('error', () => { clearTimeout(timer); reject(new Error('Unable to start EvidenceHarbor MCP bridge')); });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (exceeded || timedOut || code !== 0) return reject(new Error('EvidenceHarbor MCP bridge failed or exceeded limits; check backend connectivity and authorization'));
      try { resolve(JSON.parse(output)); } catch { reject(new Error('Invalid MCP bridge response')); }
    });
    child.stdin.end(payload);
  });
}
