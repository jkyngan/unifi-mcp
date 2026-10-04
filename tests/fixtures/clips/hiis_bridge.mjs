// Optional local source bridge. Executes only the three extracted helpers, never server/config imports.
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import vm from 'node:vm';
const source = readFileSync(process.env.HIIS_MCP_SOURCE, 'utf8');
function extract(name, end) {
  const start = source.indexOf(name === 'requestCake' ? 'async function ' + name + '(' : 'function ' + name + '(');
  const stop = source.indexOf(end, start);
  if (start < 0 || stop < 0) throw Error('helper boundaries missing');
  return source.slice(start, stop);
}
const code = [extract('requestCake', '\n/**'), extract('asImageContent', '\n/**'), extract('asFileContent', '\n// ====')].join('\n');
let input = '';
for await (const part of process.stdin) {
  input += part;
  if (input.length > 256000) throw Error('synthetic input limit');
}
const request = JSON.parse(input);
if (!["requestCake", "asImageContent", "asFileContent"].includes(request.mode)) throw Error("unsupported helper");
const context = vm.createContext({
  INTERNAL_KEY: '', CAKE_BASE_URL: 'https://synthetic.invalid', GET_TIMEOUT_MS: {},
  RETRYABLE_SOCKET_ERRORS: new Set(), URLSearchParams, URL,
  console: {warn() {}},
  cakeHttp: {get: async () => ({data: request.value})},
});
vm.runInContext(stripTypeScriptTypes(code), context, {timeout: 1000});
context.input = request.value;
const result = request.mode === 'requestCake'
  ? await vm.runInContext('requestCake("synthetic", {})', context)
  : vm.runInContext(request.mode + '(input)', context, {timeout: 1000});
process.stdout.write(JSON.stringify(result));
