// Optional REA 6 diagnostic sidecar. Invoked only by rea_browser_probe.py.
// No browser launch, profile writes, navigation, request replay, or raw output.
import http from 'node:http';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import { randomBytes } from 'node:crypto';

export const PLATFORM_ORIGINS = {
  threads: ['https://www.threads.com', 'https://www.threads.net'],
  linkedin: ['https://www.linkedin.com'],
  tiktok: ['https://www.tiktok.com'],
};
const PAGE_METHODS = new Set([
  'Page.enable', 'Page.getFrameTree', 'Page.getResourceTree',
  'Runtime.enable', 'Debugger.enable', 'Network.enable',
  'DOMSnapshot.captureSnapshot', 'Accessibility.getFullAXTree',
  'Storage.getUsageAndQuota',
  'Network.disable', 'Debugger.disable', 'Runtime.disable', 'Page.disable',
]);
const KNOWN_ROUTES = new Set([
  '/api/graphql', '/api/graphql/', '/graphql/query', '/graphql/query/',
  '/flagship-web/rsc-action/actions/pagination',
  '/api/search/general/full/', '/api/search/item/full/',
  '/api/comment/list/', '/api/comment/list/reply/', '/api/post/item_list/',
]);
const BYTE_LIMIT = 4 * 1024 * 1024;
const MESSAGE_LIMIT = 2500;
const DEADLINE_MS = 30000;

export function commandAllowed(message, targetId, sessionId, origin) {
  const p = message.params ?? {};
  if (!Number.isInteger(message.id) || !p || typeof p !== 'object') return false;
  if (message.method === 'Target.attachToTarget') {
    return !sessionId && !message.sessionId && p.targetId === targetId && p.flatten === true;
  }
  if (message.method === 'Target.detachFromTarget') {
    return !!sessionId && !message.sessionId && p.sessionId === sessionId && !p.targetId;
  }
  if (message.method === 'Target.getTargets') return !message.sessionId;
  if (!sessionId || message.sessionId !== sessionId || !PAGE_METHODS.has(message.method)) return false;
  if (message.method === 'Storage.getUsageAndQuota') return p.origin === origin;
  if (message.method === 'DOMSnapshot.captureSnapshot') {
    return Array.isArray(p.computedStyles) && p.computedStyles.length === 0
      && p.includePaintOrder === false && p.includeDOMRects === false;
  }
  return true;
}

export function compactInspection(result, origin) {
  const inspection = result.normalized_result;
  if (!inspection?.network || !inspection?.capture_window) throw new Error('unexpected_rea_result');
  const groups = new Map();
  for (const request of inspection.network.requests) {
    let path = 'other_route';
    try {
      const url = new URL(request.url);
      if (url.origin === origin && KNOWN_ROUTES.has(url.pathname)) path = url.pathname;
    } catch { /* Never return unparsed URLs. */ }
    const method = ['GET', 'POST', 'OPTIONS', 'HEAD'].includes(request.method) ? request.method : 'OTHER';
    const status = Number.isInteger(request.status) ? request.status : null;
    const key = JSON.stringify([path, method, status]);
    const group = groups.get(key) ?? { route: path, method, status, count: 0 };
    group.count += 1;
    groups.set(key, group);
  }
  return {
    status: 'ok', rea_version: '6.0.0', origin,
    observation_ms: inspection.capture_window.observation_ms,
    dom_nodes: inspection.dom.total_nodes,
    accessibility_nodes: inspection.accessibility.total_nodes,
    scripts: inspection.scripts.total,
    resources: inspection.resources.length,
    observed_requests: inspection.network.requests.length,
    request_groups: [...groups.values()].slice(0, 20),
    request_groups_omitted: Math.max(0, groups.size - 20),
    prior_network_activity_available: false,
    worker_observation_available: false,
    raw_capture_saved: false,
  };
}

async function run(config) {
  const packageRoot = resolve(config.reaPackage);
  const require = createRequire(resolve(packageRoot, 'package.json'));
  if (require('./package.json').version !== '6.0.0') throw new Error('unsupported_rea_version');
  const { WebSocket, WebSocketServer } = require('ws');
  const endpoint = new URL(config.endpoint);
  if (endpoint.protocol !== 'ws:' || endpoint.hostname !== '127.0.0.1' || !endpoint.port
      || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
    throw new Error('invalid_profile_endpoint');
  }
  const origins = PLATFORM_ORIGINS[config.platform];
  if (!origins) throw new Error('invalid_platform');
  const sockets = new Set();
  let bytes = 0;
  let messages = 0;
  let server;
  let wss;
  let timer;
  let rejectFailure;
  const failure = new Promise((_, reject) => { rejectFailure = reject; });
  // Handled immediately even if a socket fails during setup.
  failure.catch(() => {});
  const abort = new AbortController();
  const fail = (code) => {
    rejectFailure(new Error(code));
    abort.abort();
    for (const socket of sockets) socket.terminate();
  };
  const budget = (data) => {
    bytes += data.length;
    messages += 1;
    if (bytes > BYTE_LIMIT || messages > MESSAGE_LIMIT) {
      fail('capture_budget_exceeded');
      return false;
    }
    return true;
  };
  const connect = async () => {
    const socket = new WebSocket(config.endpoint, { maxPayload: BYTE_LIMIT, handshakeTimeout: 5000 });
    sockets.add(socket);
    socket.on('error', () => fail('cdp_connection_failed'));
    socket.once('close', () => sockets.delete(socket));
    await Promise.race([new Promise((done) => socket.once('open', done)), failure]);
    return socket;
  };
  timer = setTimeout(() => fail('probe_deadline_exceeded'), DEADLINE_MS);
  try {
    const discover = await connect();
    let nextId = 0;
    const pending = new Map();
    discover.on('message', (data) => {
      if (!budget(data)) return;
      let message;
      try { message = JSON.parse(data.toString()); } catch { fail('invalid_cdp_message'); return; }
      const resolveReply = pending.get(message.id);
      if (resolveReply) { pending.delete(message.id); resolveReply(message); }
    });
    const rpc = async (method) => {
      const id = ++nextId;
      const reply = new Promise((done) => pending.set(id, done));
      discover.send(JSON.stringify({ id, method }));
      const message = await Promise.race([reply, failure]);
      if (message.error) throw new Error('cdp_discovery_failed');
      return message.result;
    };
    const version = await rpc('Browser.getVersion');
    const targets = await rpc('Target.getTargets');
    const target = targets.targetInfos.find((item) => {
      try { return item.type === 'page' && origins.includes(new URL(item.url).origin); }
      catch { return false; }
    });
    discover.close();
    if (!target) throw new Error('no_existing_platform_tab');
    const url = new URL(target.url);
    if (/login|checkpoint|challenge|captcha|verification/i.test(url.pathname)) {
      throw new Error('platform_login_or_challenge');
    }
    const origin = url.origin;
    // Titles and URL parameters are unnecessary for selecting the already-bound tab.
    const safeTarget = { ...target, title: '', url: origin + '/' };
    const wsPath = '/devtools/browser/' + randomBytes(16).toString('hex');
    let address;
    server = http.createServer((request, response) => {
      if (request.method !== 'GET' || request.headers.origin || request.headers.host !== address) {
        response.writeHead(403).end(); return;
      }
      let value;
      if (request.url === '/json/version') {
        value = {
          Browser: version.product, 'Protocol-Version': version.protocolVersion,
          'User-Agent': version.userAgent, 'V8-Version': version.jsVersion,
          'WebKit-Version': version.revision,
          webSocketDebuggerUrl: `ws://${address}${wsPath}`,
        };
      } else if (request.url === '/json/list') {
        value = [{ id: target.targetId, type: 'page', title: '', url: origin + '/' }];
      } else { response.writeHead(404).end(); return; }
      response.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
      response.end(JSON.stringify(value));
    });
    wss = new WebSocketServer({ noServer: true, maxPayload: 16384 });
    let used = false;
    server.on('upgrade', (request, socket, head) => {
      if (used || request.url !== wsPath || request.headers.origin || request.headers.host !== address) {
        socket.destroy(); return;
      }
      used = true;
      wss.handleUpgrade(request, socket, head, (client) => wss.emit('connection', client));
    });
    wss.on('connection', (client) => {
      sockets.add(client);
      client.on('error', () => fail('local_bridge_failed'));
      let upstream;
      let sessionId = null;
      let attaching = false;
      const inflight = new Map();
      const ready = connect().then((socket) => {
        upstream = socket;
        socket.on('message', (data) => {
          if (!budget(data)) return;
          let message;
          try { message = JSON.parse(data.toString()); } catch { fail('invalid_cdp_message'); return; }
          if (message.id !== undefined) {
            const method = inflight.get(message.id);
            if (!method) return;
            inflight.delete(message.id);
            if (method === 'Target.attachToTarget') {
              attaching = false;
              sessionId = message.result?.sessionId ?? null;
            }
            if (method === 'Target.detachFromTarget') sessionId = null;
          } else if (!sessionId || message.sessionId !== sessionId) return;
          if (message.method === 'Page.frameNavigated' && !message.params?.frame?.parentId) {
            try {
              const next = new URL(message.params.frame.url);
              if (next.origin !== origin || /login|checkpoint|challenge|captcha|verification/i.test(next.pathname)) {
                fail('target_scope_changed'); return;
              }
            } catch { fail('target_scope_changed'); return; }
          }
          if (client.readyState === WebSocket.OPEN) client.send(JSON.stringify(message));
        });
        socket.once('close', () => client.close());
      });
      ready.catch(() => fail('cdp_connection_failed'));
      client.on('message', async (data) => {
        try {
          await ready;
          const message = JSON.parse(data.toString());
          if (!commandAllowed(message, target.targetId, sessionId, origin)
              || (attaching && message.method === 'Target.attachToTarget') || inflight.has(message.id)) {
            fail('cdp_command_denied'); return;
          }
          if (message.method === 'Target.getTargets') {
            client.send(JSON.stringify({ id: message.id, result: { targetInfos: [safeTarget] } }));
            return;
          }
          if (message.method === 'Target.attachToTarget') attaching = true;
          inflight.set(message.id, message.method);
          upstream.send(JSON.stringify(message));
        } catch { fail('bridge_command_failed'); }
      });
      client.once('close', () => { sockets.delete(client); upstream?.terminate(); });
    });
    await new Promise((done, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', done);
    });
    address = `127.0.0.1:${server.address().port}`;
    const moduleAt = (path) => import(pathToFileURL(resolve(packageRoot, path)).href);
    const [{ CdpBrowserProvider }, { inspectWebPage }] = await Promise.race([
      Promise.all([moduleAt('dist/browser/CdpBrowserProvider.js'), moduleAt('dist/application/BrowserObservationService.js')]),
      failure,
    ]);
    const result = await Promise.race([inspectWebPage(new CdpBrowserProvider(), {
      cdp_endpoint: `http://${address}`, target_id: target.targetId,
      allowed_origins: [origin], observation_ms: 500,
      include_accessibility_text: false, include_console_text: false,
      include_json_body_shapes: false, include_websocket_shapes: false,
      include_script_sources: false, include_storage_keys: false, include_storage_fingerprints: false,
    }, { signal: abort.signal }), failure]);
    if (!result.ok) throw new Error('rea_inspection_failed');
    return { ...compactInspection(result.value, origin), bridge_received_bytes: bytes };
  } finally {
    clearTimeout(timer);
    abort.abort();
    for (const socket of sockets) socket.terminate();
    wss?.close();
    server?.closeAllConnections();
    if (server?.listening) await new Promise((done) => server.close(done));
  }
}

const SAFE_ERRORS = new Set([
  'unsupported_rea_version', 'invalid_profile_endpoint', 'invalid_platform',
  'capture_budget_exceeded', 'cdp_connection_failed', 'invalid_cdp_message',
  'probe_deadline_exceeded', 'cdp_discovery_failed', 'no_existing_platform_tab',
  'platform_login_or_challenge', 'target_scope_changed', 'local_bridge_failed',
  'cdp_command_denied', 'bridge_command_failed', 'rea_inspection_failed', 'unexpected_rea_result',
]);
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    let input = '';
    for await (const chunk of process.stdin) {
      input += chunk;
      if (input.length > 8192) throw new Error('invalid_probe_input');
    }
    console.log(JSON.stringify(await run(JSON.parse(input))));
  } catch (error) {
    console.log(JSON.stringify({ status: 'blocked', reason: SAFE_ERRORS.has(error.message) ? error.message : 'probe_failed' }));
    process.exitCode = 1;
  }
}
