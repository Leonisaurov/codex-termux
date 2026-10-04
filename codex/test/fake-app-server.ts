#!/usr/bin/env bun
/**
 * Minimal app-server stand-in for `codex-ntfy-relay.ts` integration tests.
 *
 * Emits two command-approval requests (one with the argv amendment the real
 * server proposes, one without) and records every JSON-RPC message it receives
 * on stdin into `FAKE_APP_SERVER_OUT`, so the test can assert exactly what the
 * relay answered.
 */
import { appendFileSync } from "node:fs";

const out = Bun.env.FAKE_APP_SERVER_OUT ?? "";
if (!out) {
  console.error("fake-app-server: FAKE_APP_SERVER_OUT is required");
  process.exit(2);
}
const amendment = Bun.env.FAKE_APP_SERVER_AMENDMENT
  ? (JSON.parse(Bun.env.FAKE_APP_SERVER_AMENDMENT) as string[])
  : null;

const record = (value: unknown) => appendFileSync(out, `${JSON.stringify(value)}\n`);

const approval = (id: number, command: string, proposed: string[] | null) => ({
  jsonrpc: "2.0",
  id,
  method: "item/commandExecution/requestApproval",
  params: {
    threadId: "thread-1",
    turnId: "turn-1",
    itemId: `item-${id}`,
    command,
    cwd: "/tmp",
    reason: "integration test",
    proposedExecpolicyAmendment: proposed,
  },
});

console.log(JSON.stringify(approval(42, "/bin/sh -lc 'echo hola'", amendment)));
console.log(JSON.stringify(approval(43, "/bin/sh -lc 'echo sin enmienda'", null)));

const decoder = new TextDecoder();
let buffer = "";
for await (const chunk of Bun.stdin.stream()) {
  buffer += decoder.decode(chunk, { stream: true });
  const lines = buffer.split("\n");
  buffer = lines.pop() ?? "";
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      record(JSON.parse(line));
    } catch {
      record({ unparsable: line });
    }
  }
}
