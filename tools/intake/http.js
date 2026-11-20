import { createHash, timingSafeEqual } from 'node:crypto';
import { createServer } from 'node:http';

const digest = (text) => createHash('sha256').update(text).digest();
const maxBytes = 8192;

function send(res, status, body) {
  res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' });
  res.end(JSON.stringify(body));
}

async function readRequest(req) {
  const chunks = [];
  let length = 0;
  // Leave the socket open long enough to send 413 when the limit is reached.
  for await (const chunk of req.iterator({ destroyOnReturn: false })) {
    length += chunk.length;
    if (length > maxBytes) return { tooLarge: true };
    chunks.push(chunk);
  }
  try {
    const body = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
    const request = body?.request;
    if (typeof request === 'string' && [...request].length <= 1000 && request.trim()) {
      return { request: request.trim() };
    }
  } catch {
    // Invalid JSON or UTF-8 is a bad request, not a server error.
  }
  return {};
}

export function intakeServer(intake, token) {
  const expected = digest(`Bearer ${token}`);
  return createServer({ headersTimeout: 5_000, requestTimeout: 10_000 }, async (req, res) => {
    try {
      const loopback = `127.0.0.1:${req.socket.localPort}`;
      if (req.headers.host !== loopback && !(req.socket.localPort === 80 && req.headers.host === '127.0.0.1')) {
        return send(res, 421, { error: 'expected_loopback_host' });
      }
      if (!timingSafeEqual(digest(req.headers.authorization ?? ''), expected)) {
        return send(res, 401, { error: 'unauthorized' });
      }
      if (req.method === 'POST' && req.url === '/intake') {
        const key = req.headers['idempotency-key'] ?? '';
        if (!/^[\w.-]{1,128}$/.test(key)) return send(res, 400, { error: 'invalid_idempotency_key' });
        if (req.headers['content-type']?.split(';')[0].trim() !== 'application/json') {
          return send(res, 415, { error: 'expected_application_json' });
        }
        const { request, tooLarge } = await readRequest(req);
        if (tooLarge) {
          res.setHeader('connection', 'close');
          return send(res, 413, { error: 'request_too_large' });
        }
        if (!request) return send(res, 400, { error: 'invalid_request' });
        const job = intake.submit(key, request);
        if (!job) return send(res, 409, { error: 'idempotency_key_reused' });
        if (job.unavailable) return send(res, 503, { error: 'intake_unavailable' });
        res.setHeader('location', `/intake/${job.id}`);
        return send(res, 202, job);
      }
      const id = /^\/intake\/([0-9a-f-]{36})$/.exec(req.url)?.[1];
      if (req.method === 'GET' && id) {
        const job = intake.status(id);
        if (job) return send(res, 200, job);
      }
      return send(res, 404, { error: 'not_found' });
    } catch {
      if (!res.destroyed) send(res, 500, { error: 'intake_failed' });
    }
  });
}
