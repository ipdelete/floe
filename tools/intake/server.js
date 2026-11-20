import { spawnSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { createModels } from '@earendil-works/pi-ai/models';
import { openaiProvider } from '@earendil-works/pi-ai/providers/openai';
import { githubIssues } from './github.js';
import { intakeServer } from './http.js';
import { openIntake } from './intake.js';

const { INTAKE_TOKEN, GITHUB_TOKEN, OPENAI_API_KEY, INTAKE_MODEL } = process.env;
if (!INTAKE_TOKEN || INTAKE_TOKEN.length < 32 || !GITHUB_TOKEN || !OPENAI_API_KEY || !INTAKE_MODEL) {
  throw new Error('Set INTAKE_TOKEN (at least 32 characters), GITHUB_TOKEN, OPENAI_API_KEY, and INTAKE_MODEL');
}
const port = Number(process.env.INTAKE_PORT ?? 8765);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid INTAKE_PORT');

process.umask(0o077);
const directory = resolve(process.env.INTAKE_STATE_DIR ?? '.intake');
mkdirSync(directory, { recursive: true, mode: 0o700 });

function scan(text) {
  const result = spawnSync('gitleaks', ['stdin', '--redact', '--no-banner'], {
    cwd: directory,
    env: { PATH: process.env.PATH },
    input: text,
    stdio: ['pipe', 'ignore', 'ignore'],
    timeout: 5000,
    killSignal: 'SIGKILL',
  });
  if (result.status !== 0 || result.error) throw new Error('Secret scan did not pass');
}

const models = createModels();
models.setProvider(openaiProvider());
const intake = openIntake({
  directory, models, model: { provider: 'openai', modelId: INTAKE_MODEL },
  issues: githubIssues(GITHUB_TOKEN), scan,
});
intakeServer(intake, INTAKE_TOKEN).listen(port, '127.0.0.1', () => {
  console.log(`Floe intake listening at http://127.0.0.1:${port}/intake`);
  intake.start();
});
