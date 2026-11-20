import { execFileSync } from 'node:child_process';

const repo = 'ipdelete/floe';
const endpoint = `https://api.github.com/repos/${repo}/issues`;

export function issueMarker(id) {
  return `<!-- floe-intake:${id} -->`;
}

function tokenFromGh() {
  try {
    const token = execFileSync('gh', ['auth', 'token', '--hostname', 'github.com'], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 5000,
      killSignal: 'SIGKILL', maxBuffer: 4096,
    }).trim();
    if (token) return token;
  } catch {
    // Do not surface subprocess output or credentials.
  }
  throw new Error('Run gh auth login --hostname github.com or set GITHUB_TOKEN with Issues read/write access');
}

export function githubIssues(token = process.env.GITHUB_TOKEN?.trim() || tokenFromGh(), fetchIssue = fetch) {
  function request(url, options = {}) {
    return fetchIssue(url, {
      ...options,
      redirect: 'error',
      signal: AbortSignal.timeout(15_000),
      headers: {
        authorization: `Bearer ${token}`,
        accept: 'application/vnd.github+json',
        'content-type': 'application/json',
        'x-github-api-version': '2022-11-28',
      },
    });
  }

  function receipt(issue) {
    const number = issue?.number;
    if (!Number.isSafeInteger(number) || number < 1 || issue.pull_request ||
        issue.html_url !== `https://github.com/${repo}/issues/${number}`) {
      throw new Error('GitHub returned an invalid issue receipt');
    }
    return { number, url: issue.html_url };
  }

  return {
    async create(id, title, description) {
      const body = `${description}\n\nIntake session: \`${id}\`\n\n${issueMarker(id)}`;
      const response = await request(endpoint, { method: 'POST', body: JSON.stringify({ title, body }) });
      if (response.status !== 201) throw new Error('GitHub did not confirm issue creation');
      return receipt(await response.json());
    },

    async find(id) {
      const marker = issueMarker(id);
      // Direct listing avoids search-index lag. Bound recovery to the newest 1,000 entries.
      for (let page = 1; page <= 10; page++) {
        const response = await request(`${endpoint}?state=all&sort=created&direction=desc&per_page=100&page=${page}`);
        if (!response.ok) throw new Error('GitHub issue lookup failed');
        const issues = await response.json();
        if (!Array.isArray(issues)) throw new Error('GitHub returned an invalid issue list');
        const issue = issues.find((item) => !item?.pull_request && typeof item?.body === 'string' &&
          item.body.trimEnd().endsWith(`\n${marker}`));
        if (issue) return receipt(issue);
        if (issues.length < 100) break;
      }
      return null;
    },
  };
}
