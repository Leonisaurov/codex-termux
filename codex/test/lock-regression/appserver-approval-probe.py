#!/usr/bin/env python3
"""Drive a Codex `app-server` (stdio JSONL) and answer a command approval with an
execpolicy amendment, to check whether `<CODEX_HOME>/rules/default.rules` gets
written. Used by `run.sh`; see README.md in this directory."""
import json
import os
import subprocess
import sys
import threading
import time

CODEX = os.path.abspath(sys.argv[1])
HOME_DIR = os.path.abspath(sys.argv[2])
PORT = sys.argv[3]
AMENDMENT = json.loads(sys.argv[4]) if len(sys.argv) > 4 else ["echo"]

env = dict(os.environ)
env["CODEX_HOME"] = HOME_DIR
env["RUST_LOG"] = env.get("RUST_LOG", "warn")

cfg = [
    CODEX, "app-server",
    "-c", f'model_providers.fake={{name="Fake",base_url="http://127.0.0.1:{PORT}/v1",wire_api="responses",requires_openai_auth=false}}',
    "-c", "model_provider=fake",
    "-c", "model=fake-model",
    "-c", "approval_policy=untrusted",
    "-c", "sandbox_mode=danger-full-access",
    "-c", "analytics.enabled=false",
    "-c", "hide_agent_reasoning=true",
]

proc = subprocess.Popen(cfg, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, env=env, text=True, bufsize=1)

state = {"next_id": 1, "thread": None, "approvals": 0, "done": False}
lock = threading.Lock()


def send(obj):
    line = json.dumps(obj)
    print("--> " + line[:400], flush=True)
    proc.stdin.write(line + "\n")
    proc.stdin.flush()


def rpc(method, params):
    with lock:
        rid = state["next_id"]
        state["next_id"] += 1
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    return rid


def stderr_reader():
    for line in proc.stderr:
        print("[stderr] " + line.rstrip(), flush=True)


threading.Thread(target=stderr_reader, daemon=True).start()


def handle(msg):
    if "id" in msg and "method" in msg:
        method = msg["method"]
        params = msg.get("params") or {}
        print(f"<== SERVER REQUEST {method}: {json.dumps(params)[:1500]}", flush=True)
        if method == "item/commandExecution/requestApproval":
            state["approvals"] += 1
            proposed = params.get("proposedExecpolicyAmendment")
            amendment = proposed if proposed else AMENDMENT
            decision = {
                "acceptWithExecpolicyAmendment": {
                    "execpolicy_amendment": amendment,
                }
            }
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"decision": decision}})
            print(f"--> approved with amendment {amendment}", flush=True)
            if state["approvals"] >= 3:
                state["done"] = True
        elif method == "item/fileChange/requestApproval":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"decision": "accept"}})
        else:
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        return
    if "id" in msg and "result" in msg:
        print(f"<== RESULT id={msg['id']}: {json.dumps(msg['result'])[:600]}", flush=True)
        if state["thread"] is None:
            res = msg["result"]
            tid = (res.get("thread") or {}).get("id")
            if tid:
                state["thread"] = tid
                print(f"--> thread {tid}; starting turn", flush=True)
                rpc("turn/start", {
                    "threadId": tid,
                    "input": [{"type": "text", "text": "Run `touch probe-target.txt` so I can confirm."}],
                })
        return
    if "method" in msg:
        m = msg["method"]
        if m in ("turn/completed", "thread/status/changed"):
            print(f"<== NOTIFY {m}: {json.dumps(msg.get('params'))[:300]}", flush=True)
        if m == "turn/completed":
            state["done"] = True
        return


def stdout_reader():
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            print("<== RAW " + line[:300], flush=True)
            continue
        if "method" in msg and msg.get("method") in ("item/agentMessage/delta",):
            continue
        print("<== " + json.dumps(msg)[:800], flush=True)
        try:
            handle(msg)
        except Exception as exc:  # noqa
            print(f"handler error: {exc}", flush=True)


threading.Thread(target=stdout_reader, daemon=True).start()

rpc("initialize", {"clientInfo": {"name": "hermes-probe", "version": "0.0.1"},
                   "capabilities": {"experimentalApi": True}})
time.sleep(0.5)
send({"jsonrpc": "2.0", "method": "initialized", "params": None})
time.sleep(0.5)
rpc("thread/start", {})

deadline = time.time() + 120
while time.time() < deadline and not state["done"]:
    time.sleep(1)

print(f"\n=== approvals answered: {state['approvals']}", flush=True)
rules = os.path.join(HOME_DIR, "rules", "default.rules")
print(f"=== {rules} exists: {os.path.exists(rules)}", flush=True)
if os.path.exists(rules):
    with open(rules) as fh:
        print(fh.read(), flush=True)
try:
    proc.terminate()
except Exception:
    pass
