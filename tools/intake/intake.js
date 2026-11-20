import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { BACKGROUND_CONTEXT as context } from '@earendil-works/chord/context';
import { createRegistry, defineExtension, defineTool, Harness } from '@earendil-works/pi-durable';
import { openNodeSqliteStorage } from '@earendil-works/pi-durable/storage/sqlite/node';
import { obj, str } from '@earendil-works/pi-schema';

const instructions = `You are Floe's feature intake agent. The user message is the maintainer's feature request.
Create one issue with a short title and a short description of the requested behavior.
Do not invent scope, requirements, or implementation steps. Omit private details and credentials.
Use create_feature_issue to publish the draft; do not claim publication without a tool receipt.`;

export function openIntake({ directory, models, model, issues, scan }) {
  // Hold a lock on a separate database for the service lifetime. A process exit releases it.
  const lock = new DatabaseSync(join(directory, 'worker.sqlite'), { timeout: 0 });
  let db;
  try {
    lock.exec('BEGIN EXCLUSIVE');
    db = new DatabaseSync(join(directory, 'jobs.sqlite'));
    db.exec(`CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, request TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued', attempted INTEGER NOT NULL DEFAULT 0,
      issue_number INTEGER, issue_url TEXT, reason TEXT
    )`);
  } catch (error) {
    db?.close();
    lock.close();
    throw error;
  }
  const byId = db.prepare('SELECT * FROM jobs WHERE id = ?');
  const byKey = db.prepare('SELECT * FROM jobs WHERE key = ?');
  const next = db.prepare("SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY rowid LIMIT 1");
  const setStatus = db.prepare('UPDATE jobs SET status = ?, reason = ? WHERE id = ?');
  const markAttempt = db.prepare('UPDATE jobs SET attempted = 1 WHERE id = ? AND attempted = 0');
  const saveIssue = db.prepare('UPDATE jobs SET issue_number = ?, issue_url = ? WHERE id = ?');
  const addJob = db.prepare('INSERT INTO jobs (id, key, request) VALUES (?, ?, ?)');
  let worker;
  let stopping = false;

  function record(id, issue) {
    saveIssue.run(issue.number, issue.url, id);
    return issue;
  }

  async function checkText(id, text) {
    try {
      await scan(text);
    } catch {
      setStatus.run('blocked', 'secret_scan_failed', id);
      throw new Error('Secret scan did not pass; this intake is blocked');
    }
  }

  async function publish(id, title, description) {
    const job = byId.get(id);
    if (job.issue_url) return { number: job.issue_number, url: job.issue_url };
    if (job.reason) throw new Error('This intake is blocked');
    if (job.attempted) {
      const found = await issues.find(id);
      if (found) return record(id, found);
      throw new Error('Issue creation is unconfirmed; do not retry');
    }
    title = title.trim();
    description = description.trim();
    if (!title || title.length > 120 || /[\r\n]/.test(title) || !description || description.length > 2000) {
      throw new Error('Use a one-line title (up to 120 characters) and a short description (up to 2000)');
    }
    await checkText(id, `${title}\n${description}`);
    // Record intent before the only POST. An interrupted or uncertain attempt can only be looked up.
    if (!markAttempt.run(id).changes) throw new Error('Issue publication is already in progress');
    return record(id, await issues.create(id, title, description));
  }

  async function run(job) {
    const tool = defineTool({
      name: 'create_feature_issue',
      description: 'Publish the concise feature issue for this intake session.',
      parameters: obj({ title: str(), description: str() }),
      callers: ['model'],
      replay: 'safe',
      async execute({ title, description }) {
        const issue = await publish(job.id, title, description);
        return {
          output: [{ type: 'text', text: JSON.stringify({ issue_number: issue.number, issue_url: issue.url }) }],
          control: { terminate: true },
        };
      },
    });
    const registry = createRegistry();
    registry.install(defineExtension({ name: 'intake', tools: [tool] }));
    const storage = await openNodeSqliteStorage(join(directory, `${job.id}.sqlite`));
    const harness = await Harness.open(storage, {
      models, registry, settings: { toolExecution: 'sequential', stream: { timeoutMs: 60_000 } },
    }, context);
    try {
      const root = await harness.root(context, { agent: { model, instructions, maxTurns: 3 } });
      const submission = await root.submit({ type: 'input', content: job.request, requestId: job.id }, context);
      await submission.wait(context);
    } finally {
      await harness.close(context);
    }
  }

  async function drain() {
    while (!stopping) {
      const job = next.get();
      if (!job) return;
      setStatus.run('running', null, job.id);
      try {
        if (!job.attempted) await checkText(job.id, job.request);
        await run(job);
      } catch {
        // Never expose request text, provider diagnostics, or credentials in HTTP responses or logs.
      }
      let current = byId.get(job.id);
      if (current.attempted && !current.issue_url) {
        try {
          const found = await issues.find(job.id);
          if (found) record(job.id, found);
        } catch {
          // A failed lookup is not evidence that the issue was never created.
        }
        current = byId.get(job.id);
      }
      const reason = current.attempted ? 'issue_creation_unconfirmed' : current.reason ?? 'no_issue_published';
      setStatus.run(current.issue_url ? 'completed' : 'blocked', current.issue_url ? null : reason, job.id);
    }
  }

  function start() {
    if (!worker && !stopping) {
      worker = Promise.resolve().then(drain).catch(() => {
        // Unexpected host storage failures should restart the service, not claim success.
        console.error('Intake storage failed; restart the service.');
        stopping = true;
      }).finally(() => {
        worker = undefined;
        if (!stopping && next.get()) start();
      });
    }
  }

  function status(id) {
    const job = byId.get(id);
    if (!job) return null;
    return {
      id, status: job.status,
      ...(job.issue_url && { issue_number: job.issue_number, issue_url: job.issue_url }),
      ...(job.reason && { reason: job.reason }),
    };
  }

  return {
    start,
    status,
    submit(key, request) {
      if (stopping) return { unavailable: true };
      const previous = byKey.get(key);
      if (previous) return previous.request === request ? status(previous.id) : null;
      const id = randomUUID();
      addJob.run(id, key, request);
      start();
      return status(id);
    },
    async close() {
      stopping = true;
      await worker;
      db.close();
      lock.close();
    },
  };
}
