/**
 * Pure approval-decision builders for the Codex app-server JSONL protocol.
 *
 * Kept separate from `codex-ntfy-relay.ts` (which has top-level side effects:
 * spawning the app-server and serving the panel) so the wire contract stays
 * unit-testable. See `codex/test/codex-ntfy-decisions.test.ts`.
 */

export type Json = Record<string, unknown>;

/** Approval choices the relay can send back to the app-server. */
export type ApprovalAction = "allow" | "session" | "deny" | "deny-and-steer" | "persist";

const DECISIONS: Record<Exclude<ApprovalAction, "persist">, string> = {
  allow: "accept",
  session: "acceptForSession",
  deny: "decline",
  "deny-and-steer": "decline",
};

/**
 * Build the JSON-RPC response for a pending approval request.
 *
 * `persist` answers with `acceptWithExecpolicyAmendment`, which makes Codex
 * append an `allow` prefix rule to `<CODEX_HOME>/rules/default.rules` so the
 * command keeps being allowed in later sessions. The amendment must be the argv
 * prefix proposed by the app-server: Codex writes it verbatim as the rule
 * pattern, so inventing a shorter prefix would allow commands the user never
 * approved.
 */
export function approvalResponse(
  rpcId: unknown,
  action: string,
  proposedAmendment: readonly string[] | null | undefined,
): Json {
  if (action === "persist") {
    if (!proposedAmendment || proposedAmendment.length === 0) {
      throw new Error("persist_requires_amendment");
    }
    return {
      jsonrpc: "2.0",
      id: rpcId,
      result: {
        decision: {
          acceptWithExecpolicyAmendment: {
            execpolicy_amendment: [...proposedAmendment],
          },
        },
      },
    };
  }

  const decision = DECISIONS[action as Exclude<ApprovalAction, "persist">];
  if (!decision) throw new Error(`unknown_action:${action}`);
  return { jsonrpc: "2.0", id: rpcId, result: { decision } };
}

/** Extract the argv-prefix amendment the app-server proposed, if any. */
export function proposedAmendment(params: Json): string[] | null {
  const raw = params.proposedExecpolicyAmendment;
  if (!Array.isArray(raw)) return null;
  const tokens = raw.filter((token): token is string => typeof token === "string");
  return tokens.length === raw.length && tokens.length > 0 ? tokens : null;
}
