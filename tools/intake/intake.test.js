import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { get } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import test from 'node:test';
import { fauxAssistantMessage } from '@earendil-works/pi-ai/providers/faux';
import { githubIssues, issueMarker } from './github.js';
import { intakeServer } from './http.js';
import { openIntake } from './intake.js';
import { createDraft, draftCall, fakeModel, issue, searchCall, toolTurn } from './fixtures/fake.js';

async function directory(t) {
  const path = await mkdtemp(join(tmpdir(), 'floe-intake-'));
  t.after(() => rm(path, { recursive: true, force: true }));
  return path;
}

async function waitForJob(readStatus) {
  for (let i = 0; i < 250; i++) {
    const job = await readStatus();
    if (['completed', 'duplicate', 'blocked'].includes(job.status)) return job;
    await sleep(20);
  }
  assert.fail('Intake did not settle');
}

test('uses gh credentials unless an issue token is explicitly configured', async (t) => {
  const dir = await directory(t);
  await writeFile(join(dir, 'gh'), `#!/bin/sh
[ "$*" = 'auth token --hostname github.com' ] || exit 1
printf 'test-cli-token\\n'
`, { mode: 0o700 });
  const probe = spawnSync(process.execPath, ['--input-type=module', '-e', `
    import { githubIssues } from './github.js';
    const request = async (_url, options) => {
      console.log(options.headers.authorization);
      return Response.json([]);
    };
    await githubIssues(undefined, request).find('test');
    process.env.GITHUB_TOKEN = 'test-explicit-token';
    await githubIssues(undefined, request).find('test');
  `], { cwd: new URL('.', import.meta.url), env: { PATH: dir }, encoding: 'utf8', timeout: 5000 });
  assert.equal(probe.status, 0, probe.stderr);
  assert.equal(probe.stdout, 'Bearer test-cli-token\nBearer test-explicit-token\n');
});

test('authenticated HTTP intake searches the repo before publishing one issue', async (t) => {
  const dir = await directory(t);
  const calls = [];
  const issues = githubIssues('github-token', async (url, opts) => {
    if (!opts.method) {
      calls.push('search');
      const target = new URL(url);
      assert.equal(target.origin + target.pathname, 'https://api.github.com/search/issues');
      assert.equal(target.searchParams.get('q'), 'repo:ipdelete/floe is:issue ("skip" OR "tasks")');
      return Response.json({ incomplete_results: false, items: [] });
    }
    calls.push('create');
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
  assert.deepEqual(calls, ['search', 'create']);
  assert.deepEqual(offered, ['search_features', 'create_feature_issue']);
  assert.equal(input, 'Skip some tasks');
});

for (const [decision, outcome] of [
  [{ duplicate: issue.number }, { status: 'duplicate', issue_number: issue.number, issue_url: issue.url }],
  [{ possible_duplicate: true }, { status: 'blocked', reason: 'possible_duplicate' }],
  [{ duplicate: 999 }, { status: 'blocked', reason: 'no_issue_published' }],
]) {
  test(`deduplication decision ${JSON.stringify(decision)} never creates an issue`, async (t) => {
    const dir = await directory(t);
    const candidate = { ...issue, title: 'Skip a workflow task', state: 'closed' };
    let posts = 0;
    let evidence;
    const intake = openIntake({
      directory: dir,
      ...fakeModel((context) => {
        evidence = context.messages.find((message) => message.role === 'toolResult');
        return fauxAssistantMessage(JSON.stringify(decision));
      }),
      issues: {
        search: () => [{ ...candidate, excerpt: 'Allow a task to be skipped.' }],
        create: () => { posts++; return issue; },
      },
      scan: () => {},
    });
    t.after(() => intake.close());
    const { id } = intake.submit('feature-1', 'Skip some tasks');
    const expected = decision.possible_duplicate ? { ...outcome, matches: [candidate] } : outcome;
    assert.deepEqual(await waitForJob(() => intake.status(id)), { id, ...expected });
    assert.match(evidence.content[0].text, /Allow a task to be skipped\./);
    assert.equal(posts, 0);
  });
}

for (const scenario of ['no-search', 'same-round', 'failed-search']) {
  test(`does not publish with ${scenario}`, async (t) => {
    const dir = await directory(t);
    let posts = 0;
    let searches = 0;
    const first = scenario === 'no-search' ? createDraft()
      : scenario === 'same-round' ? toolTurn([searchCall, draftCall]) : toolTurn(searchCall);
    const second = scenario === 'failed-search' ? createDraft() : fauxAssistantMessage('Done');
    const issues = githubIssues('github-token', async (_url, options) => {
      if (options.method === 'POST') posts++;
      else searches++;
      return Response.json({ incomplete_results: scenario === 'failed-search', items: [] });
    });
    const intake = openIntake({ directory: dir, ...fakeModel(second, first), issues, scan: () => {} });
    const { id } = intake.submit('feature-1', 'Skip some tasks');
    // For a failed search, the job is marked blocked immediately. Let the attempted next call finish too.
    const job = await waitForJob(() => intake.status(id)).finally(() => intake.close());
    assert.deepEqual(job, {
      id, status: 'blocked', reason: scenario === 'failed-search' ? 'feature_search_failed' : 'no_issue_published',
    });
    assert.equal(searches, scenario === 'no-search' ? 0 : 1);
    assert.equal(posts, 0);
  });
}

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
      issues: { search: () => [], create: () => { posts++; return issue; } },
      scan: () => { if (++scans === (check === 'request' ? 1 : 3)) throw new Error('test finding'); },
    });
    t.after(() => intake.close());
    const { id } = intake.submit('feature-1', 'Skip some tasks');
    assert.deepEqual(await waitForJob(() => intake.status(id)), { id, status: 'blocked', reason: 'secret_scan_failed' });
    assert.equal(posts, 0);
    assert.equal(calls > 0, check === 'draft');
  });
}
