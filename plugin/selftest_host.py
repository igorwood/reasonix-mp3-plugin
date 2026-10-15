#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mp3-player sidecar 自测：模拟 Reasonix host 走完整 Extension Protocol v2 流程。

用法: python3 selftest_host.py
会真播几秒音乐（验证 afplay），并在 <workspace>/.selftest-home 下写测试用 playlist.json。
"""

import glob
import json
import os
import select
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(ROOT)
SELFTEST_HOME = os.path.join(WS, ".selftest-home")
SUBDIR = os.path.join(WS, "selftest-tmp")
FAKE_PICK = os.path.join(WS, ".selftest-pick.txt")
OTHER_MP3 = sorted(glob.glob(os.path.join(WS, "*.mp3")))[0]  # 工作区里的真 mp3（用于替换测试）
EXTRA_MP3 = os.path.join(SUBDIR, "extra.mp3")

shutil.rmtree(SELFTEST_HOME, ignore_errors=True)
os.makedirs(SUBDIR, exist_ok=True)
with open(EXTRA_MP3, "wb") as fh:
    fh.write(b"\x00" * 64)  # 假 mp3：只验证列表加入，不播放

env = dict(os.environ)
env["REASONIX_HOME"] = SELFTEST_HOME
env["REASONIX_PLUGIN_NAME"] = "mp3-player"
env["MP3PLAYER_FAKE_PICK"] = FAKE_PICK

PROC = subprocess.Popen(
    [sys.executable, os.path.join(ROOT, "sidecar.py")],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1, env=env,
)

results = []
TRACE = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  <- " + str(extra)) if extra and not cond else ""))


def send(obj):
    PROC.stdin.write(json.dumps(obj) + "\n")
    PROC.stdin.flush()


def respond(req_id, result):
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def recv(timeout=5.0):
    r, _, _ = select.select([PROC.stdout], [], [], timeout)
    if not r:
        return None
    line = PROC.stdout.readline()
    if not line:
        return None
    return json.loads(line)


def next_frame(predicate, timeout=8.0):
    """读到第一个满足 predicate 的帧，途中 publish 请求一律应答。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        f = recv(max(0.1, deadline - time.time()))
        if f is None:
            continue
        TRACE.append(f)
        if f.get("method") == "host/ui/publish":
            respond(f["id"], {"accepted": True})
        if predicate(f):
            return f
    return None


def call_response(req_id, timeout=8.0):
    return next_frame(lambda f: f.get("id") == req_id and ("result" in f or "error" in f), timeout)


def is_publish(f, kind):
    return f.get("method") == "host/ui/publish" and f.get("params", {}).get("kind") == kind


# ---------------------------------------------------------------- 测试流程

print("== 1. initialize")
send({"jsonrpc": "2.0", "id": 1, "method": "extension/initialize", "params": {
    "protocolId": "reasonix.extension.v2", "protocolVersion": "2",
    "session": {"sessionId": "selftest", "generation": 1, "workspaceRoot": WS},
    "capabilities": {}, "manifest": {},
}})
r = call_response(1)
res = (r or {}).get("result", {})
check("InitializeResult 必需字段齐全",
      res.get("name") == "mp3-player" and str(res.get("protocolVersion")) == "2"
      and res.get("stateSchemaVersion") == 0 and bool(res.get("version")))
actions = {a["actionId"] for a in res.get("uiActions", [])}
check("uiActions 含 toggle/mode/add", actions == {"toggle", "mode", "add"}, actions)

print("== 2. initialized -> status chip + 极简卡片")
send({"jsonrpc": "2.0", "method": "extension/initialized", "params": {}})
status = next_frame(lambda f: is_publish(f, "status"))
card = next_frame(lambda f: is_publish(f, "card"))
sl = (status or {}).get("params", {}).get("payload", {})
cl = (card or {}).get("params", {}).get("payload", {})
check("初始 chip label 为 🎵", sl.get("label") == "🎵", sl.get("label"))
card_actions = [a["actionId"] for a in cl.get("actions", [])]
check("卡片三按钮 toggle+mode+add", card_actions == ["toggle", "mode", "add"], card_actions)
check("卡片无 fields（精简）", "fields" not in cl)

print("== 2a. 预载播放列表（＋ 选择一批）")
with open(FAKE_PICK, "w", encoding="utf-8") as fh:
    fh.write(EXTRA_MP3 + "\n")
send({"jsonrpc": "2.0", "id": 20, "method": "extension/ui/action",
      "params": {"actionId": "add", "sessionId": "selftest", "generation": 1}})
call_response(20)
note = next_frame(lambda f: is_publish(f, "notification"))
check("载入后通知「播放列表已更新」", note and "播放列表已更新" in note["params"]["payload"].get("title", ""),
      note["params"]["payload"].get("title") if note else "no notification")
pf = os.path.join(SELFTEST_HOME, "mp3-player", "playlist.json")
saved = json.load(open(pf, encoding="utf-8")) if os.path.exists(pf) else {}
check("playlist.json 记录所选文件", any(p.endswith("extra.mp3") for p in saved.get("tracks", [])), saved)

print("== 2b. session.start 事件 -> 用真实会话重新发布")
send({"jsonrpc": "2.0", "method": "extension/event", "params": {
    "event": "session.start",
    "payload": {"session": {"sessionId": "real-sess", "generation": 7, "workspaceRoot": WS}}}})
st2 = next_frame(lambda f: is_publish(f, "status") and f["params"].get("sessionId") == "real-sess")
check("session.start 后以真实会话发布 status", st2 is not None,
      st2["params"].get("sessionId") if st2 else "no status for real-sess")

print("== 2c. extension/intercept(session.start) -> continue + 真实会话发布")
send({"jsonrpc": "2.0", "id": 10, "method": "extension/intercept", "params": {
    "event": "session.start", "seq": 1, "timeoutMillis": 5000,
    "payload": {"session": {"sessionId": "real-sess2", "generation": 9, "workspaceRoot": WS}}}})
r = call_response(10)
check("intercept 回 decision=continue",
      ((r or {}).get("result") or {}).get("decision") == "continue", (r or {}).get("result"))
st3 = next_frame(lambda f: is_publish(f, "status") and f["params"].get("sessionId") == "real-sess2")
check("intercept 后以真实会话发布 status", st3 is not None,
      st3["params"].get("sessionId") if st3 else "no status for real-sess2")

print("== 3. toggle -> 播放，chip 图标切换为 ▶")
send({"jsonrpc": "2.0", "id": 2, "method": "extension/ui/action",
      "params": {"actionId": "toggle", "sessionId": "selftest", "generation": 1}})
r = call_response(2)
check("toggle 回 accepted", bool((r or {}).get("result", {}).get("accepted")))
st = next_frame(lambda f: is_publish(f, "status") and f["params"]["surfaceId"] == "mp3-player-status"
                and "播放中" in f["params"]["payload"].get("detail", ""))
check("播放中 chip detail 含「播放中」", st is not None, TRACE[-6:])
time.sleep(0.5)

print("== 3b. play:0 -> 播放指定曲目")
send({"jsonrpc": "2.0", "id": 11, "method": "extension/ui/action",
      "params": {"actionId": "play:0", "sessionId": "selftest", "generation": 1}})
r = call_response(11)
check("play:0 回 accepted", bool((r or {}).get("result", {}).get("accepted")))

print("== 3c. mode 切换 随机 -> 顺序")
send({"jsonrpc": "2.0", "id": 12, "method": "extension/ui/action",
      "params": {"actionId": "mode", "sessionId": "selftest", "generation": 1}})
r = call_response(12)
check("mode 回 accepted", bool((r or {}).get("result", {}).get("accepted")))
st_mode = next_frame(lambda f: is_publish(f, "status") and "顺序" in f["params"]["payload"].get("detail", ""))
check("切到顺序后 detail 含「顺序」", st_mode is not None)

print("== 4. 再次选择不同的 mp3 -> 替换旧列表")
with open(FAKE_PICK, "w", encoding="utf-8") as fh:
    fh.write(OTHER_MP3 + "\n" + EXTRA_MP3 + "\n")
send({"jsonrpc": "2.0", "id": 3, "method": "extension/ui/action",
      "params": {"actionId": "add", "sessionId": "selftest", "generation": 1}})
r = call_response(3)
check("add 回 accepted", bool((r or {}).get("result", {}).get("accepted")))
note = next_frame(lambda f: is_publish(f, "notification"))
check("替换后通知「播放列表已更新」", note and "播放列表已更新" in note["params"]["payload"].get("title", ""),
      note["params"]["payload"].get("title") if note else "no notification")
saved = json.load(open(pf, encoding="utf-8")) if os.path.exists(pf) else {}
check("旧列表被替换为新的 2 首",
      len(saved.get("tracks", [])) == 2
      and any(p.endswith(os.path.basename(OTHER_MP3)) for p in saved["tracks"])
      and any(p.endswith("extra.mp3") for p in saved["tracks"]), saved)

print("== 5. 非法输入被拒绝（非 mp3 / 不存在）")
with open(FAKE_PICK, "w", encoding="utf-8") as fh:
    fh.write("/tmp/x.txt\n/nope/y.mp3\n")
send({"jsonrpc": "2.0", "id": 4, "method": "extension/ui/action",
      "params": {"actionId": "add", "sessionId": "selftest", "generation": 1}})
call_response(4)
note = next_frame(lambda f: is_publish(f, "notification"))
check("拒绝非 mp3 / 不存在并提示",
      note and "没有载入任何文件" in note["params"]["payload"].get("title", ""),
      note["params"]["payload"].get("title") if note else "no notification")

print("== 6. 空选择（取消）不崩溃")
with open(FAKE_PICK, "w", encoding="utf-8") as fh:
    fh.write("")
send({"jsonrpc": "2.0", "id": 5, "method": "extension/ui/action",
      "params": {"actionId": "add", "sessionId": "selftest", "generation": 1}})
call_response(5)
note = next_frame(lambda f: is_publish(f, "notification"))
check("空选择给出提示且进程存活", note is not None and PROC.poll() is None)

print("== 7. 确保播放 -> toggle 停止")
send({"jsonrpc": "2.0", "id": 6, "method": "extension/ui/action",
      "params": {"actionId": "play:0", "sessionId": "selftest", "generation": 1}})
call_response(6)
next_frame(lambda f: is_publish(f, "status") and "播放中" in f["params"]["payload"].get("detail", ""))
send({"jsonrpc": "2.0", "id": 16, "method": "extension/ui/action",
      "params": {"actionId": "toggle", "sessionId": "selftest", "generation": 1}})
call_response(16)
st = next_frame(lambda f: is_publish(f, "status")
                and "已停止" in f["params"]["payload"].get("detail", ""))
check("停止后 chip detail 含「已停止」", st is not None, TRACE[-6:])

print("== 8. shutdown")
send({"jsonrpc": "2.0", "id": 7, "method": "extension/shutdown", "params": {"timeoutMillis": 2000}})
r = call_response(7)
check("shutdown 回 accepted", bool((r or {}).get("result", {}).get("accepted")))
try:
    PROC.wait(timeout=5)
    check("sidecar 已退出", True)
except subprocess.TimeoutExpired:
    check("sidecar 已退出", False)
    PROC.kill()

err = PROC.stderr.read()
if "Traceback" in err:
    print("  (stderr 含 Traceback)\n" + err)

shutil.rmtree(SUBDIR, ignore_errors=True)
try:
    os.remove(FAKE_PICK)
except OSError:
    pass
shutil.rmtree(SELFTEST_HOME, ignore_errors=True)

passed = sum(1 for _, ok in results if ok)
print("\n%d/%d PASSED" % (passed, len(results)))
sys.exit(0 if passed == len(results) else 1)
