"""Run a deterministic provider process at the app-server protocol boundary."""

import json
import os
import subprocess
import sys
import time


def send(value):
    print(json.dumps(value), flush=True)


if "--version" in sys.argv:
    print("codex-cli 0.153.4")
    raise SystemExit(0)

threads = {}
statuses = {}
configurations = {}
terminals: dict[str, list[dict[str, str]]] = {}
cleanup_mode = {}
cleanup_requests: dict[str, int] = {}
turn_counts: dict[str, int] = {}
last_requests = {}
next_results: dict[str, object] = {}
count = 0
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    params = message.get("params", {})
    request_id = message.get("id")
    if method in {"thread/start", "thread/resume", "turn/start", "turn/steer"}:
        last_requests[method] = params
    if method in next_results:
        send({"id": request_id, "result": next_results.pop(method)})
    elif method == "initialize":
        send({"id": request_id, "result": {"userAgent": "fixture/0.153.4"}})
    elif method == "thread/start":
        count += 1
        thread_id = f"thread-{count}"
        threads[thread_id] = params["cwd"]
        statuses[thread_id] = "idle"
        configurations[thread_id] = params
        terminals[thread_id] = []
        send({"id": request_id, "result": {"thread": {"id": thread_id}}})
    elif method == "thread/resume":
        send({"id": request_id, "result": {"thread": {"id": params["threadId"]}}})
    elif method == "thread/read":
        thread_id = params["threadId"]
        send(
            {
                "id": request_id,
                "result": {
                    "thread": {
                        "id": thread_id,
                        "status": {"type": statuses[thread_id]},
                    }
                },
            }
        )
    elif method == "fixture/status":
        statuses[params["threadId"]] = params["status"]
        send({"id": request_id, "result": {}})
    elif method == "fixture/configuration":
        send({"id": request_id, "result": configurations[params["threadId"]]})
    elif method == "fixture/last-request":
        send({"id": request_id, "result": last_requests.get(params["method"], {})})
    elif method == "fixture/message":
        send(params["message"])
        send({"id": request_id, "result": {}})
    elif method == "fixture/next-result":
        next_results[params["method"]] = params["result"]
        send({"id": request_id, "result": {}})
    elif method == "fixture/environment":
        send({"id": request_id, "result": {key: os.environ.get(key) for key in params["names"]}})
    elif method == "fixture/background":
        thread_id = params["threadId"]
        terminals[thread_id] = [{"processId": "101", "command": "private-background-command"}]
        cleanup_mode[thread_id] = params.get("mode", "clean")
        send({"id": request_id, "result": {}})
    elif method == "thread/backgroundTerminals/clean":
        thread_id = params["threadId"]
        cleanup_requests[thread_id] = cleanup_requests.get(thread_id, 0) + 1
        mode = cleanup_mode.get(thread_id, "clean")
        if mode == "timeout":
            time.sleep(2)
        if mode == "error":
            send({"id": request_id, "error": {"code": -32000, "message": "fixture cleanup"}})
        else:
            if mode == "clean":
                terminals[thread_id] = []
            send({"id": request_id, "result": {}})
    elif method == "thread/backgroundTerminals/list":
        if cleanup_mode.get(params["threadId"]) == "list_error":
            send({"id": request_id, "error": {"code": -32000, "message": "fixture inventory"}})
        else:
            send(
                {
                    "id": request_id,
                    "result": {"data": terminals[params["threadId"]], "nextCursor": None},
                }
            )
    elif method == "fixture/cleanup-count":
        send({"id": request_id, "result": {"count": cleanup_requests.get(params["threadId"], 0)}})
    elif method == "fixture/notifications":
        for _ in range(params["count"]):
            send(
                {
                    "method": params["method"],
                    "params": {
                        "threadId": params["threadId"],
                        "turnId": params.get("turnId", "turn-1"),
                        "delta": "private-stream-text",
                        "tokenUsage": {"totalTokens": 123456789},
                    },
                }
            )
        send({"id": request_id, "result": {}})
    elif method in {"turn/start", "turn/steer"}:
        thread_id = params["threadId"]
        if method == "turn/start":
            turn_counts[thread_id] = turn_counts.get(thread_id, 0) + 1
        turn_id = f"turn-{turn_counts[thread_id]}"
        text = params["input"][0]["text"]
        statuses[thread_id] = "active"
        if text == "crash":
            raise SystemExit(0)
        if text == "orphan":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            send(
                {
                    "id": request_id,
                    "result": {"turn": {"id": turn_id}, "childPid": child.pid},
                }
            )
            raise SystemExit(0)
        send({"id": request_id, "result": {"turn": {"id": turn_id}}})
        send({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id}}})
        if text == "approval":
            send(
                {
                    "id": "approve-1",
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "itemId": "item-1",
                        "command": "echo private-command",
                    },
                }
            )
        elif text == "input":
            send(
                {
                    "id": "input-1",
                    "method": "item/tool/requestUserInput",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "itemId": "item-2",
                        "questions": [{"id": "choice", "question": "private-question"}],
                    },
                }
            )
        elif text == "tool":
            send(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {
                            "id": "item-3",
                            "type": "commandExecution",
                            "command": "private-command",
                        },
                    },
                }
            )
        else:
            send(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "delta": "private-answer",
                    },
                }
            )
            statuses[thread_id] = "idle"
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {"id": turn_id, "status": "completed"},
                    },
                }
            )
    elif method == "fixture/freeze":
        send({"id": request_id, "result": {}})
        time.sleep(2)
    elif method == "turn/interrupt":
        statuses[params["threadId"]] = "idle"
        send({"id": request_id, "result": {}})
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": params["threadId"],
                    "turn": {"id": params["turnId"], "status": "interrupted"},
                },
            }
        )
    elif method is None and request_id in {"approve-1", "input-1"}:
        statuses["thread-1"] = "idle"
        send(
            {
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
            }
        )
    elif request_id is not None:
        send({"id": request_id, "error": {"code": -32601, "message": "unsupported fixture method"}})
