import { afterAll, expect, test } from "bun:test";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/**
 * End-to-end test of `codex-ntfy-relay.ts` against a stand-in app-server: the
 * relay itself, its HTTP panel and its JSON-RPC plumbing are real; only Codex
 * and ntfy are faked. Covers the "Permitir siempre (regla)" path, which must
 * forward the app-server's proposed argv amendment verbatim.
 */

const CODEX_DIR = join(import.meta.dir, "..");
const RELAY = join(CODEX_DIR, "more", "codex-ntfy-relay.ts");
const FAKE_SERVER = join(import.meta.dir, "fake-app-server.ts");
const AMENDMENT = ["/bin/sh", "-lc", "echo hola"];

const work = mkdtempSync(join(tmpdir(), "codex-relay-test-"));
const received = join(work, "received.jsonl");

const freePort = (): number => {
  const probe = Bun.serve({ port: 0, fetch: () => new Response("probe") });
  const port = probe.port;
  probe.stop(true);
  return port;
};

const ntfy = Bun.serve({ port: 0, fetch: () => new Response("ok") });
const hookPort = freePort();

const relay = Bun.spawn(["bun", RELAY], {
  cwd: CODEX_DIR,
  stdin: "pipe",
  stdout: "ignore",
  stderr: "pipe",
  env: {
    ...process.env,
    CODEX_NTFY_CODEX_BIN: "bun",
    CODEX_NTFY_CODEX_ARGS: FAKE_SERVER,
    NTFY_URL: `http://127.0.0.1:${ntfy.port}`,
    NTFY_TOPIC: "codex-test",
    CODEX_NTFY_PUBLISH_TOKEN: "publish-token",
    CODEX_NTFY_HOOK_TOKEN: "hook-token",
    CODEX_NTFY_HOOK_PORT: String(hookPort),
    CODEX_NTFY_CALLBACK_HOST: "127.0.0.1",
    CODEX_NTFY_APPROVAL_TTL_MS: "0",
    FAKE_APP_SERVER_OUT: received,
    FAKE_APP_SERVER_AMENDMENT: JSON.stringify(AMENDMENT),
  },
});

let panelUrl = "";
const stderrLines: string[] = [];
void (async () => {
  const decoder = new TextDecoder();
  let buffer = "";
  for await (const chunk of relay.stderr) {
    buffer += decoder.decode(chunk, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      stderrLines.push(line);
      const match = line.match(/panel vivo (http:\/\/\S+)/);
      if (match?.[1]) panelUrl = match[1];
    }
  }
})();

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

async function waitFor<T>(label: string, probe: () => Promise<T | null> | T | null, timeoutMs = 15000): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await probe();
    if (value !== null && value !== undefined) return value;
    if (Date.now() > deadline) {
      throw new Error(`timeout waiting for ${label}\nstderr:\n${stderrLines.join("\n")}`);
    }
    await sleep(100);
  }
}

const state = async () => {
  if (!panelUrl) return null;
  const response = await fetch(`${panelUrl}/state`);
  return response.ok ? await response.json() : null;
};

const receivedMessages = () => {
  if (!existsSync(received)) return [];
  return readFileSync(received, "utf8")
    .split("\n")
    .filter(Boolean)
    .map(line => JSON.parse(line) as Record<string, unknown>);
};

const act = async (id: string, action: string) => {
  const response = await fetch(`${panelUrl}/action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, action }),
  });
  return { status: response.status, body: (await response.json()) as Record<string, unknown> };
};

afterAll(() => {
  relay.kill();
  ntfy.stop(true);
  rmSync(work, { recursive: true, force: true });
});

test("panel exposes both pending approvals and the persist flag", async () => {
  const pending = await waitFor("two pending approvals", async () => {
    const snapshot = (await state()) as { pending?: unknown[]; persistRules?: boolean } | null;
    return snapshot?.pending && snapshot.pending.length === 2 ? snapshot : null;
  });
  expect((pending as { persistRules: boolean }).persistRules).toBe(true);
  const ids = (pending as { pending: { id: string }[] }).pending.map(item => item.id).sort();
  expect(ids).toEqual(["42", "43"]);
});

test("persist forwards the proposed amendment to the app-server", async () => {
  await waitFor("pending approvals", async () => ((await state()) ? true : null));
  const result = await act("42", "persist");
  expect(result).toEqual({ status: 200, body: { ok: true } });

  const response = await waitFor("amendment response", () =>
    receivedMessages().find(message => message.id === 42) ?? null,
  );
  expect(response).toEqual({
    jsonrpc: "2.0",
    id: 42,
    result: {
      decision: {
        acceptWithExecpolicyAmendment: {
          execpolicy_amendment: AMENDMENT,
        },
      },
    },
  });
});

test("persist is refused when the app-server proposed no amendment", async () => {
  await waitFor("pending approvals", async () => ((await state()) ? true : null));
  const result = await act("43", "persist");
  expect(result.status).toBe(400);
  expect(result.body).toEqual({ ok: false, reason: "no_proposed_amendment" });
});

test("plain accept still answers the legacy decision string", async () => {
  await waitFor("pending approvals", async () => ((await state()) ? true : null));
  const result = await act("43", "allow");
  expect(result).toEqual({ status: 200, body: { ok: true } });

  const response = await waitFor("accept response", () =>
    receivedMessages().find(message => message.id === 43) ?? null,
  );
  expect(response).toEqual({ jsonrpc: "2.0", id: 43, result: { decision: "accept" } });
});
