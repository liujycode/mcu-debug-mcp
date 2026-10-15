# -*- coding: utf-8 -*-
"""标准 MCP 握手检查（CI/发布验收用）
用法：python mcp_handshake_check.py <mcu-debug-mcp 可执行文件路径>
验收：initialize + tools/list(59) + resources/list(8) + templates/list(8) + prompts/list(6)
"""
import json
import subprocess
import sys
import time

EXPECT = {"tools": 59, "resources": 8, "templates": 8, "prompts": 6}

MSG = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "acceptance", "version": "0.1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
    {"jsonrpc": "2.0", "id": 4, "method": "resources/templates/list"},
    {"jsonrpc": "2.0", "id": 5, "method": "prompts/list"},
]


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else "mcu-debug-mcp"
    p = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    for m in MSG:
        p.stdin.write((json.dumps(m) + "\n").encode())
        p.stdin.flush()
        time.sleep(0.15)
    replies = {}
    t0 = time.time()
    while time.time() - t0 < 30 and set(replies) < {1, 2, 3, 4, 5}:
        line = p.stdout.readline()
        if not line:
            break
        try:
            d = json.loads(line.decode(errors="replace"))
        except json.JSONDecodeError:
            continue
        if "id" in d:
            replies[d["id"]] = d
    p.stdin.close()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()

    ok = bool(replies.get(1, {}).get("result", {}).get("serverInfo", {}).get("name"))
    got = {
        "tools": len(replies.get(2, {}).get("result", {}).get("tools", [])),
        "resources": len(replies.get(3, {}).get("result", {}).get("resources", [])),
        "templates": len(replies.get(4, {}).get("result", {}).get("resourceTemplates", [])),
        "prompts": len(replies.get(5, {}).get("result", {}).get("prompts", [])),
    }
    for k, want in EXPECT.items():
        mark = "PASS" if got[k] == want else "FAIL"
        if got[k] != want:
            ok = False
        print("[%s] %-10s %d/%s" % (mark, k, got[k], want))
    print("===== %s =====" % ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
