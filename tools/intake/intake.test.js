import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { get } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import test from 'node:test';
import { githubIssues, issueMarker } from './github.js';
import { intakeServer } from './http.js';
import { openIntake } from './intake.js';
import { createDraft, fakeModel, issue } from './fixtures/fake.js';

async function directory(t) {
  const path = await mkdtemp(join(tmpdir(), 'floe-intake-'));
  t.after(() => rm(path, { recursive: true, force: true }));
  return path;
}

async function waitForJob(readStatus) {
  for (let i = 0; i < 250; i++) {
    const job = await readStatus();
    if (['completed', 'blocked'].includes(job.status)) return job;
    await sleep(20);
  }
  assert.fail('Intake did not settle');
}

test('authenticated HTTP intake persists one job and offers only the issue tool', async (t) => {
  const dir = await directory(t);
  let posts = 0;
  const issues = githubIssues('github-token', async (url, opts) => {
    posts++;
    assert.equal(url, 'https://api.github.com/repos/ipdelete/floe/issues');
    assert.equal(opts.headers.authorization, 'Bearer github-token');
    assert.equal(opts.method, 'POST');
    const draft = JSON.parse(opts.body);
    assert.equal(draft.title, 'Skip tasks');
    assert.match(draft.body, /^Skip some tasks\.\n\nIntake session: `[\w-]+`\n\n<!-- floe-intake:[\w-]+ -->$/);
    return Response.json({ number: issue.number, html_url: issue.url }, { status: 201 });
  });
  let offered;
  let input;
  const model = fakeModel((context) => {
    offered = context.messages.flatMap((message) => message.toolsAdded ?? []).map((tool) => tool.name);
    input = context.messages.find((message) => message.role === 'user')?.content;
    return createDraft();
  });
  const options = { directory: dir, ...model, issues, scan: () => {} };
  const intake = openIntake(options);
  t.after(() => intake.close());
  assert.throws(() => openIntake(options), /locked/);
  const server = intakeServer(intake, 'test-token').listen(0, '127.0.0.1');
  t.after(() => new Promise((resolve) => server.close(resolve)));
  await once(server, 'listening');
  const base = `http://127.0.0.1:${server.address().port}`;
  const headers = { authorization: 'Bearer test-token', 'content-type': 'application/json', 'idempotency-key': 'feature-1' };
  const post = (body, requestHeaders = headers) => fetch(`${base}/intake`, { method: 'POST', headers: requestHeaders, body });
  assert.equal((await post('{}', {})).status, 401);
  const [wrongHost] = await once(get(`${base}/intake`, { headers: { ...headers, host: 'wrong.example' } }), 'response');
  wrongHost.resume();
  assert.equal(wrongHost.statusCode, 421);
  assert.equal((await post('{')).status, 400);
  assert.equal((await post(JSON.stringify({ request: 'x'.repeat(9000) }))).status, 413);
  const request = JSON.stringify({ request: 'Skip some tasks' });
  const accepted = await post(request);
  assert.equal(accepted.status, 202);
  const { id } = await accepted.json();
  const statusUrl = `${base}${accepted.headers.get('location')}`;
  const retry = await post(request);
  assert.equal((await retry.json()).id, id);
  assert.equal((await post(JSON.stringify({ request: 'Different feature' }))).status, 409);
  const job = await waitForJob(async () => (await fetch(statusUrl, { headers })).json());
  assert.deepEqual(job, { id, status: 'completed', issue_number: issue.number, issue_url: issue.url });
  assert.equal(posts, 1);
  assert.deepEqual(offered, ['create_feature_issue']);
  assert.equal(input, 'Skip some tasks');
});

for (const found of [true, false]) {
  test(`recovers a crashed create without another POST (marker ${found ? 'present' : 'absent'})`, async (t) => {
    const dir = await directory(t);
    const crashed = spawnSync(process.execPath, ['fixtures/crash.js', dir], {
      cwd: new URL('.', import.meta.url), encoding: 'utf8', timeout: 5000,
    });
    assert.equal(crashed.status, 44, crashed.stderr);
    const id = crashed.stdout.trim();
    let posts = 0;
    let lookups = 0;
    const issues = githubIssues('github-token', async (url, opts) => {
      if (opts.method === 'POST') posts++;
      lookups++;
      assert.match(url, /state=all/);
      return Response.json(found ? [{ number: issue.number, html_url: issue.url, body: `Draft\r\n${issueMarker(id)}\r\n` }] : []);
    });
    const intake = openIntake({ directory: dir, ...fakeModel(), issues, scan: () => { throw new Error('scanner unavailable after restart'); } });
    t.after(() => intake.close());
    intake.start();
    const job = await waitForJob(() => intake.status(id));
    assert.ok(lookups > 0);
    assert.equal(posts, 0);
    assert.deepEqual(job, found ? { id, status: 'completed', issue_number: issue.number, issue_url: issue.url }
      : { id, status: 'blocked', reason: 'issue_creation_unconfirmed' });
    assert.deepEqual(intake.submit('feature-1', 'Skip some tasks'), job);
  });
}

for (const check of ['request', 'draft']) {
  test(`failed ${check} scan blocks publication`, async (t) => {
    const dir = await directory(t);
    let scans = 0;
    let calls = 0;
    let posts = 0;
    const intake = openIntake({
      directory: dir,
      ...fakeModel(() => { calls++; return createDraft(); }),
      issues: { create: () => { posts++; return issue; } },
      scan: () => { if (++scans === (check === 'request' ? 1 : 2)) throw new Error('test finding'); },
    });
    t.after(() => intake.close());
    const { id } = intake.submit('feature-1', 'Skip some tasks');
    assert.deepEqual(await waitForJob(() => intake.status(id)), { id, status: 'blocked', reason: 'secret_scan_failed' });
    assert.equal(posts, 0);
    assert.equal(calls > 0, check === 'draft');
  });
}
