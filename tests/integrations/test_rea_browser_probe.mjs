import assert from 'node:assert/strict';
import test from 'node:test';
import { commandAllowed, compactInspection } from '../../tools/rea_browser_probe.mjs';

const target = 'selected-target';
const session = 'owned-session';
const origin = 'https://www.threads.com';
const command = (method, params = {}, sessionId = session) => ({
  id: 1, method, params, ...(sessionId === undefined ? {} : { sessionId }),
});

test('mutation methods cannot pass the guard through an owned page session', () => {
  for (const method of [
    'Page.navigate', 'Page.close', 'Runtime.evaluate', 'Runtime.callFunctionOn',
    'Network.setExtraHTTPHeaders', 'Storage.clearDataForOrigin',
    'Input.dispatchMouseEvent', 'Browser.close', 'Target.createTarget',
  ]) {
    assert.equal(commandAllowed(command(method), target, session, origin), false, method);
  }
});

test('page reads require the owned flat session, including cleanup', () => {
  for (const method of ['Page.getFrameTree', 'Page.getResourceTree', 'Network.disable']) {
    assert.equal(commandAllowed(command(method), target, session, origin), true);
    assert.equal(commandAllowed(command(method, {}, 'foreign-session'), target, session, origin), false);
    assert.equal(commandAllowed({ id: 1, method, params: {} }, target, session, origin), false);
    assert.equal(commandAllowed(command(method), target, null, origin), false);
  }
});

test('attachment cannot select another target or replace an existing session', () => {
  const attach = { id: 1, method: 'Target.attachToTarget', params: { targetId: target, flatten: true } };
  assert.equal(commandAllowed(attach, target, null, origin), true);
  assert.equal(commandAllowed({ ...attach, params: { ...attach.params, targetId: 'foreign-target' } }, target, null, origin), false);
  assert.equal(commandAllowed({ ...attach, params: { ...attach.params, flatten: false } }, target, null, origin), false);
  assert.equal(commandAllowed({ ...attach, sessionId: 'foreign-session' }, target, null, origin), false);
  assert.equal(commandAllowed(attach, target, session, origin), false);
});

test('detach cannot release another session or select a target instead', () => {
  const detach = { id: 1, method: 'Target.detachFromTarget', params: { sessionId: session } };
  assert.equal(commandAllowed(detach, target, session, origin), true);
  assert.equal(commandAllowed({ ...detach, params: { sessionId: 'foreign-session' } }, target, session, origin), false);
  assert.equal(commandAllowed({ ...detach, params: { sessionId: session, targetId: 'foreign-target' } }, target, session, origin), false);
  assert.equal(commandAllowed({ ...detach, sessionId: session }, target, session, origin), false);
  assert.equal(commandAllowed(detach, target, null, origin), false);
});

test('storage and DOM snapshot requests cannot widen the selected read options', () => {
  assert.equal(commandAllowed(command('Storage.getUsageAndQuota', { origin }), target, session, origin), true);
  assert.equal(commandAllowed(command('Storage.getUsageAndQuota', { origin: 'https://foreign.example' }), target, session, origin), false);
  const snapshot = { computedStyles: [], includePaintOrder: false, includeDOMRects: false };
  assert.equal(commandAllowed(command('DOMSnapshot.captureSnapshot', snapshot), target, session, origin), true);
  assert.equal(commandAllowed(command('DOMSnapshot.captureSnapshot', { ...snapshot, computedStyles: ['display'] }), target, session, origin), false);
  assert.equal(commandAllowed(command('DOMSnapshot.captureSnapshot', { ...snapshot, includeDOMRects: true }), target, session, origin), false);
});

test('compact output excludes sensitive markers and reports omitted request groups', () => {
  const marker = 'SYNTHETIC_CREDENTIAL_MARKER';
  const requests = Array.from({ length: 25 }, (_, index) => ({
    url: `${origin}/graphql/query?access_token=${marker}#${marker}`,
    method: 'POST', status: 200 + index,
    initiator: { url: `${origin}/${marker}`, text: marker },
    body_shapes: { request: { secret: marker }, response: { secret: marker } },
  }));
  requests.push(
    { url: `${origin}/${marker}?token=${marker}#${marker}`, method: marker, status: null },
    { url: `https://${marker}:${marker}@foreign.example/graphql/query`, method: 'GET', status: 200 },
    { url: `${origin}/graphql/query?token=${marker}#${marker}`, method: 'POST', status: 200 },
  );
  const input = {
    raw_result: { secret: marker },
    parameters: { cookie: marker },
    normalized_result: {
      target: { title: marker, url: `${origin}/?token=${marker}` },
      capture_window: { observation_ms: 500 },
      dom: { total_nodes: 4, nodes: [{ text: marker }] },
      accessibility: { total_nodes: 3, nodes: [{ name: marker, description: marker }] },
      scripts: { total: 2, items: [{ source: marker }] },
      resources: [{ url: `${origin}/${marker}` }],
      network: { requests }, console: { events: [{ text: marker }] },
      metadata: { auth: marker }, storage: { cookie: marker }, limitations: [marker],
    },
  };
  const output = compactInspection(input, origin);
  assert.equal(JSON.stringify(output).includes(marker), false);
  assert.equal(output.observed_requests, 28);
  assert.equal(output.request_groups.length, 20);
  assert.equal(output.request_groups_omitted, 7);
  assert.deepEqual(output.request_groups[0], { route: '/graphql/query', method: 'POST', status: 200, count: 2 });
  assert.equal(output.raw_capture_saved, false);
  assert.equal(output.prior_network_activity_available, false);
});

test('unknown, cross-origin and malformed routes never become output paths', () => {
  const marker = 'SYNTHETIC_CREDENTIAL_MARKER';
  const input = { normalized_result: {
    capture_window: { observation_ms: 500 }, dom: { total_nodes: 0 },
    accessibility: { total_nodes: 0 }, scripts: { total: 0 }, resources: [],
    network: { requests: [
      { url: `${origin}/${marker}`, method: 'GET', status: 200 },
      { url: `https://foreign.example/graphql/query?token=${marker}`, method: 'GET', status: 200 },
      { url: marker, method: 'GET', status: 200 },
    ] },
  } };
  const output = compactInspection(input, origin);
  assert.deepEqual(output.request_groups, [{ route: 'other_route', method: 'GET', status: 200, count: 3 }]);
  assert.equal(JSON.stringify(output).includes(marker), false);
});
