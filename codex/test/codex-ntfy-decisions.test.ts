import { describe, expect, test } from "bun:test";

import { approvalResponse } from "../more/codex-ntfy-decisions";

// The `persist` shape is the contract the app-server v2 protocol accepts for
// `item/commandExecution/requestApproval` responses (externally tagged enum +
// snake_case field inside the struct variant, value = argv prefix array). It was
// verified against codex 0.134.0-alpha.3 by answering a real approval request.
const PROPOSED = ["/bin/sh", "-lc", "echo hola > target/approved.txt"];

describe("approvalResponse", () => {
  test("accept keeps the plain decision string", () => {
    expect(approvalResponse(7, "allow", PROPOSED)).toEqual({
      jsonrpc: "2.0",
      id: 7,
      result: { decision: "accept" },
    });
  });

  test("session maps to acceptForSession", () => {
    expect(approvalResponse("rpc-1", "session", null)).toEqual({
      jsonrpc: "2.0",
      id: "rpc-1",
      result: { decision: "acceptForSession" },
    });
  });

  test("deny actions map to decline", () => {
    for (const action of ["deny", "deny-and-steer"]) {
      expect(approvalResponse(1, action, null)).toEqual({
        jsonrpc: "2.0",
        id: 1,
        result: { decision: "decline" },
      });
    }
  });

  test("persist sends the proposed amendment so the rule is written to disk", () => {
    expect(approvalResponse(9, "persist", PROPOSED)).toEqual({
      jsonrpc: "2.0",
      id: 9,
      result: {
        decision: {
          acceptWithExecpolicyAmendment: {
            execpolicy_amendment: PROPOSED,
          },
        },
      },
    });
  });

  test("persist copies the amendment instead of aliasing caller state", () => {
    const amendment = [...PROPOSED];
    const response = approvalResponse(1, "persist", amendment) as {
      result: { decision: { acceptWithExecpolicyAmendment: { execpolicy_amendment: string[] } } };
    };
    amendment.push("mutated");
    expect(response.result.decision.acceptWithExecpolicyAmendment.execpolicy_amendment).toEqual(
      PROPOSED,
    );
  });

  test("persist without a proposed amendment is rejected", () => {
    expect(() => approvalResponse(1, "persist", null)).toThrow("persist_requires_amendment");
    expect(() => approvalResponse(1, "persist", [])).toThrow("persist_requires_amendment");
  });

  test("unknown actions are rejected instead of silently cancelling", () => {
    expect(() => approvalResponse(1, "explode", null)).toThrow("unknown_action");
  });
});
