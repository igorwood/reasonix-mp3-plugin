#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""mp3-player — Reasonix Extension Protocol v2 sidecar (v2).

界面形态（按用户要求精简）：
  低栏 status chip : 只做显示 —— "♪▶"（播放中）/ "♪⏹"（停止），曲名在 tooltip，不可点击
  卡片 card        : 只有两个按钮 —— 「播放/停止」和「＋ 添加 mp3」
  「＋ 添加」       : 弹输入框（host/ui/request + form），粘贴一个或多个 mp3 路径，
                     校验后缀与存在性后加入持久播放列表（REASONIX_HOME/mp3-player/playlist.json）
播放：macOS 自带 afplay，播完自动续下一首。
"""

import glob
import json
import os
import random
import subprocess
import sys
import threading
import time

PLUGIN_NAME = os.environ.get("REASONIX_PLUGIN_NAME", "mp3-player")
PLUGIN_VERSION = "0.3.0"
STATUS_SURFACE = "mp3-player-status"
CARD_SURFACE = "mp3-player-card"
FORM_SURFACE = "mp3-player-add-form"
AFPLAY = "/usr/bin/afplay"
NAME_MAX = 34  # chip 里曲名截断长度
SESSION_EVENTS = ("session.start", "session.load", "session.rotate")

_log_lock = threading.Lock()
_log_paths_cache = None


def _log_paths():
    global _log_paths_cache
    if _log_paths_cache is None:
        paths = []
        try:
            d = os.path.join(home_dir(), "mp3-player")
            os.makedirs(d, exist_ok=True)
            paths.append(os.path.join(d, "sidecar.log"))
        except OSError:
            pass
        try:
            paths.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sidecar.log"))
        except Exception:  # noqa: BLE001
            pass
        try:
            paths.append("/tmp/mp3-player-sidecar.log")
        except Exception:  # noqa: BLE001
            pass
        _log_paths_cache = paths
    return _log_paths_cache


def log(msg):
    line = "[%s] %s" % (PLUGIN_NAME, msg)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with _log_lock:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        for path in _log_paths():
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("%s %s\n" % (stamp, line))
            except OSError:
                pass


def home_dir():
    return os.environ.get("REASONIX_HOME") or os.path.join(os.path.expanduser("~"), ".reasonix")


def playlist_path():
    d = os.path.join(home_dir(), "mp3-player")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return os.path.join(d, "playlist.json")


def _as_apple_str(value):
    """把字符串安全嵌入 AppleScript 双引号字面量。"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def short_name(path, limit=NAME_MAX):
    name = os.path.basename(path)
    if len(name) <= limit:
        return name
    return name[: limit - 1] + "…"


# ---------------------------------------------------------------- Player


class Player(object):
    def __init__(self, workspace):
        self.workspace = workspace
        self.selection = []          # 用户通过「＋」选择的文件（持久化）
        self.tracks = []             # 播放列表（= selection）
        self.index = -1
        self.proc = None
        self.playing = False
        self.shuffle = True   # 默认随机
        self._gen = 0
        self._lock = threading.Lock()
        self.on_change = None

    # -- 列表维护（播放列表完全由用户「＋」选择的文件决定）
    def _rebuild(self):
        with self._lock:
            self.tracks = list(self.selection)

    def load_playlist(self):
        try:
            with open(playlist_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            sel = data.get("tracks", [])
            if isinstance(sel, list):
                self.selection = [p for p in sel if isinstance(p, str) and os.path.isfile(p)]
        except (OSError, ValueError):
            self.selection = []
        self._rebuild()

    def save_playlist(self):
        try:
            with open(playlist_path(), "w", encoding="utf-8") as fh:
                json.dump({"tracks": self.selection}, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            log("save playlist failed: %r" % exc)

    def set_selection(self, paths):
        """用新选择**替换**整个播放列表。返回 (accepted, rejected)。"""
        accepted, rejected, seen = [], [], set()
        for raw in paths:
            cand = (raw or "").strip().strip('"').strip("'")
            if not cand:
                continue
            if cand.startswith("~"):
                cand = os.path.expanduser(cand)
            elif not os.path.isabs(cand):
                cand = os.path.join(self.workspace, cand)
            if not cand.lower().endswith(".mp3"):
                rejected.append("%s（不是 mp3）" % cand)
                continue
            if not os.path.isfile(cand):
                rejected.append("%s（文件不存在）" % cand)
                continue
            rp = os.path.realpath(cand)
            if rp in seen:
                continue
            seen.add(rp)
            accepted.append(os.path.abspath(cand))
        if accepted:
            with self._lock:
                self.selection = accepted
                self._kill_locked()   # 列表变了，停止当前播放
                self.index = -1
            self._rebuild()
            self.save_playlist()
        return accepted, rejected

    # -- 播放
    def _notify(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception as exc:  # noqa: BLE001
                log("on_change error: %r" % exc)

    def _kill_locked(self):
        self._gen += 1
        proc = self.proc
        self.proc = None
        self.playing = False
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    def _watch(self, gen):
        proc = self.proc
        if proc is None:
            return
        proc.wait()
        with self._lock:
            if gen != self._gen:
                return
            self.playing = False
            self.proc = None
        log("track finished -> auto next")
        self.next()

    def _play_index(self, i):
        with self._lock:
            if not self.tracks:
                return False
            self._kill_locked()
            self.index = i % len(self.tracks)
            path = self.tracks[self.index]
            try:
                self.proc = subprocess.Popen(
                    [AFPLAY, path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:  # noqa: BLE001
                log("afplay failed: %r" % exc)
                self.proc = None
                return False
            self.playing = True
            self._gen += 1
            gen = self._gen
        threading.Thread(target=self._watch, args=(gen,), daemon=True).start()
        self._notify()
        return True

    def random_play(self):
        """随机播放一首（尽量不同于当前）。"""
        with self._lock:
            n = len(self.tracks)
            cur = self.index
        if n == 0:
            return False
        if n == 1:
            return self._play_index(0)
        choices = [i for i in range(n) if i != cur]
        return self._play_index(random.choice(choices))

    def play(self):
        """开始播放：随机模式随机选一首，顺序模式从当前位置继续。"""
        if self.playing:
            return True
        with self._lock:
            shuf = self.shuffle
            cur = self.index
            n = len(self.tracks)
        if n == 0:
            return False
        if shuf:
            return self.random_play()
        if 0 <= cur < n:
            return self._play_index(cur)
        return self._play_index(0)

    def stop(self):
        with self._lock:
            self._kill_locked()
        self._notify()

    def toggle(self):
        if self.playing:
            self.stop()
            return "stopped"
        self.play()
        return "playing"

    def next(self):
        """播完自动续播：随机模式随机，顺序模式下一首。"""
        with self._lock:
            shuf = self.shuffle
            n = len(self.tracks)
            cur = self.index
        if n == 0:
            return False
        if shuf:
            return self.random_play()
        return self._play_index((cur if cur >= 0 else -1) + 1)

    def state(self):
        with self._lock:
            return {
                "tracks": list(self.tracks),
                "index": self.index,
                "playing": self.playing,
                "shuffle": self.shuffle,
                "count": len(self.tracks),
            }

    def shutdown(self):
        with self._lock:
            self._kill_locked()


# ---------------------------------------------------------------- Sidecar


class Sidecar(object):
    def __init__(self):
        self._out_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_id = 0
        self._pending = {}  # id -> [threading.Event, result]
        self._pending_lock = threading.Lock()
        self.session = None
        self.player = None

    # -- wire
    def _write(self, frame):
        with self._out_lock:
            sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def _reply(self, req_id, result):
        self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _reply_error(self, req_id, code, message):
        self._write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    def _notify_host(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _call_host(self, method, params, timeout=300.0):
        """发请求给 host 并同步等待响应（仅用于 host/ui/request）。"""
        rid = self._bump()
        ev = threading.Event()
        with self._pending_lock:
            self._pending[rid] = [ev, None]
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not ev.wait(timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            return None
        with self._pending_lock:
            return self._pending.pop(rid, [None, None])[1]

    def _on_response(self, frame):
        rid = frame.get("id")
        mark = None
        with self._pending_lock:
            slot = self._pending.get(rid)
            if not slot:
                return
            # publish 登记（第三项是 kind:surfaceId）：记录结果后删除，不阻塞
            if len(slot) >= 3 and slot[2]:
                mark = slot[2]
                del self._pending[rid]
            else:
                slot[1] = frame.get("result")
                slot[0].set()
        if mark:
            if frame.get("error"):
                log("PUBLISH REJECTED %s: %s" % (mark, self._json(frame.get("error"))))
            else:
                log("publish ok: %s" % mark)

    # -- UI 发布
    def _publish(self, surface_id, kind, payload):
        if not self.session:
            return
        rid = self._bump()
        with self._pending_lock:
            self._pending[rid] = [None, None, "%s:%s" % (kind, surface_id)]
        self._write({
            "jsonrpc": "2.0",
            "id": rid,
            "method": "host/ui/publish",
            "params": {
                "sessionId": self.session["sessionId"],
                "generation": self.session["generation"],
                "surfaceId": surface_id,
                "kind": kind,
                "payload": payload,
            },
        })

    def _bump(self):
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def publish_status(self):
        """低栏 chip：只显示一个音符图标（♪），状态与曲名放在 tooltip。"""
        if not self.session or not self.player:
            return
        st = self.player.state()
        if st["count"] == 0:
            label, detail = "🎵", "无 mp3 · 点卡片「＋ 添加 mp3」"
        else:
            label = "🎵"
            idx = st["index"] if 0 <= st["index"] < st["count"] else 0
            detail = "%s · [%d/%d] · %s · %s" % (
                short_name(st["tracks"][idx]), idx + 1, st["count"],
                "随机" if st.get("shuffle") else "顺序",
                "播放中" if st["playing"] else "已停止")
        self._publish(STATUS_SURFACE, "status", {"label": label, "detail": detail, "severity": "info"})

    def publish_card(self):
        """极简卡片：播放/停止 + 添加 mp3。"""
        self._publish(CARD_SURFACE, "card", {
            "title": "🎵 mp3 播放器",
            "actions": [
                {"actionId": "toggle", "label": "▶ 播放 / ⏹ 停止"},
                {"actionId": "mode", "label": "🔀 / 🔁"},
                {"actionId": "add", "label": "＋ 添加 mp3"},
            ],
        })

    def notify(self, title, body="", severity="info"):
        self._publish("mp3-player-notice", "notification",
                      {"title": title, "body": body, "severity": severity})

    def refresh_ui(self):
        self.publish_status()
        self.publish_card()

    # -- handlers
    def handle_initialize(self, req_id, params):
        session = params.get("session", {})
        self.session = {
            "sessionId": session["sessionId"],
            "generation": session["generation"],
            "workspaceRoot": session["workspaceRoot"],
        }
        log("initialize: session=%s gen=%s ws=%s" % (
            self.session["sessionId"], self.session["generation"], self.session["workspaceRoot"]))
        self.player = Player(self.session["workspaceRoot"])
        self.player.on_change = self.refresh_ui
        self.player.load_playlist()
        self._reply(req_id, {
            "name": PLUGIN_NAME,
            "protocolVersion": params.get("protocolVersion", "2"),
            "stateSchemaVersion": 0,
            "version": PLUGIN_VERSION,
            "subscriptions": ["session.start", "session.load", "session.rotate"],
            "replaces": [],
            "providers": [],
            "uiActions": [
                {"actionId": "toggle", "label": "播放/停止"},
                {"actionId": "mode", "label": "🔀/🔁"},
                {"actionId": "add", "label": "添加 mp3"},
            ],
        })

    def handle_initialized(self):
        log("initialized -> publish status chip + card (boot session)")
        self.publish_status()
        self.publish_card()
        threading.Thread(target=self._republish_loop, daemon=True).start()

    def _republish_loop(self):
        """兜底：前端可能在 runtime 构建早期丢弃 surface；持续重发一段时间覆盖就绪窗口。"""
        time.sleep(3.0)
        for _ in range(15):
            if not self.session:
                return
            self.publish_status()
            self.publish_card()
            time.sleep(2.0)

    def handle_event(self, params):
        """观察事件（extension/event，非阻塞）。"""
        event = params.get("event") or ""
        payload = params.get("payload") or {}
        log("event: %s payload=%s" % (event, self._json(payload)))
        if event in SESSION_EVENTS:
            self._apply_session_event(payload)

    def handle_intercept(self, req_id, params):
        """拦截事件（extension/intercept，阻塞；须在 timeoutMillis 内响应）。"""
        event = params.get("event") or ""
        payload = params.get("payload") or {}
        log("intercept: %s timeout=%s payload=%s" % (
            event, params.get("timeoutMillis"), self._json(payload)))
        # 先回 decision（尽快放行，不拖慢阻塞热路径），再执行副作用
        self._reply(req_id, {"decision": "continue"})
        if event in SESSION_EVENTS:
            self._apply_session_event(payload)

    def _apply_session_event(self, payload):
        """从 session.* 事件 payload 提取会话信息，更新后重发 status/card。

        官方模式（fullsidecar）：status/card 在 session.start 时发布并绑定活动会话；
        仅用 initialize 的 boot 会话发布不会出现在用户 tab 上。
        """
        sess = None
        if isinstance(payload, dict):
            for key in ("session", "sessionContext", "context", "meta"):
                v = payload.get(key)
                if isinstance(v, dict) and v.get("sessionId"):
                    sess = v
                    break
            if sess is None and payload.get("sessionId"):
                sess = payload
        if sess and self.session:
            old_ws = self.session.get("workspaceRoot")
            self.session["sessionId"] = sess.get("sessionId") or self.session["sessionId"]
            if isinstance(sess.get("generation"), int):
                self.session["generation"] = sess["generation"]
            if sess.get("workspaceRoot"):
                self.session["workspaceRoot"] = sess["workspaceRoot"]
            log("session updated: id=%s gen=%s ws=%s" % (
                self.session["sessionId"], self.session["generation"], self.session["workspaceRoot"]))
            if self.player and self.session["workspaceRoot"] != old_ws:
                self.player.workspace = self.session["workspaceRoot"]
        self.publish_status()
        self.publish_card()

    @staticmethod
    def _json(obj):
        try:
            return json.dumps(obj, ensure_ascii=False)[:400]
        except Exception:  # noqa: BLE001
            return repr(obj)[:400]

    def handle_ui_action(self, req_id, params):
        action_id = params.get("actionId", "")
        log("ui action: %s" % action_id)
        # 先回 response，再执行副作用 —— 保证 RPC 响应先于 publish 帧写出
        if action_id == "toggle":
            self._reply(req_id, {"accepted": True, "message": "已切换播放状态"})
            self.player.toggle()
            return
        if action_id.startswith("play:"):
            self._reply(req_id, {"accepted": True, "message": "播放指定曲目"})
            try:
                idx = int(action_id.split(":", 1)[1])
            except ValueError:
                return
            self.player._play_index(idx)
            self.refresh_ui()
            return
        if action_id == "mode":
            self._reply(req_id, {"accepted": True, "message": ""})
            self.player.shuffle = not self.player.shuffle
            log("shuffle -> %s" % self.player.shuffle)
            if self.player.state()["count"] > 1:
                self.player.random_play()   # 点标签总是随机换一首
            else:
                self.refresh_ui()
            return
        if action_id == "add":
            self._reply(req_id, {"accepted": True, "message": "请在系统文件选择器中选择 mp3"})
            threading.Thread(target=self._run_add, daemon=True).start()
            return
        if action_id == "noop":
            self._reply(req_id, {"accepted": True, "message": ""})
            return
        self._reply_error(req_id, -32602, "unknown action: %s" % action_id)

    def _run_add(self):
        """弹出 macOS 原生文件选择器（osascript），把选中的 mp3 加入播放列表。"""
        if not self.session:
            return
        out, err, rc = self._pick_files()
        log("add: pick rc=%s out=%r err=%r" % (rc, (out or "")[:300], (err or "")[:200]))
        if rc != 0 or not out.strip():
            low = err.lower()
            if "cancel" in low or "(-128)" in low:
                log("add: cancelled by user")
                return
            log("add: pick failed rc=%s err=%s" % (rc, err.strip()[:200]))
            self.notify("添加 mp3 失败", (err.strip() or "没有选择文件")[:200], "warn")
            return
        parts = [ln.strip().strip('"') for ln in out.splitlines() if ln.strip()]
        log("add: parts=%r" % parts[:6])
        accepted, rejected = self.player.set_selection(parts)
        log("add: accepted=%d rejected=%d %r" % (len(accepted), len(rejected), rejected[:6]))
        if accepted:
            self.notify("播放列表已更新（%d 首）" % len(accepted),
                        "\n".join(short_name(p) for p in accepted[:8]), "info")
        elif rejected:
            self.notify("没有载入任何文件", "\n".join(rejected[:5]), "warn")
        else:
            self.notify("没有选择任何文件", "", "warn")
        self.refresh_ui()

    def _pick_files(self):
        """调用系统文件选择器，返回 (stdout, stderr, returncode)。

        测试用：环境变量 MP3PLAYER_FAKE_PICK 指向一个文件，其内容即"选中的路径"（每行一个）。
        """
        fake = os.environ.get("MP3PLAYER_FAKE_PICK")
        if fake:
            try:
                with open(fake, encoding="utf-8") as fh:
                    return fh.read(), "", 0
            except OSError as exc:
                return "", str(exc), 1
        # 默认定位到当前播放文件所在目录
        default_dir = None
        st = self.player.state() if self.player else None
        if st and st["tracks"]:
            idx = st["index"] if 0 <= st["index"] < len(st["tracks"]) else 0
            cand = os.path.dirname(st["tracks"][idx])
            if os.path.isdir(cand):
                default_dir = cand
        elif self.player and os.path.isdir(self.player.workspace):
            default_dir = self.player.workspace   # 列表为空时定位到工作区
        loc = " default location (POSIX file %s)" % _as_apple_str(default_dir) if default_dir else ""
        script = (
            'set fs to choose file with prompt %s%s '
            'of type {"mp3"} with multiple selections allowed\n'
            'set out to ""\n'
            'repeat with f in fs\n'
            '  set out to out & (POSIX path of f) & linefeed\n'
            'end repeat\n'
            'return out'
        ) % (_as_apple_str("选择要添加的 mp3 文件"), loc)
        try:
            proc = subprocess.run(["osascript", "-e", script],
                                  capture_output=True, text=True, timeout=900)
            return proc.stdout, proc.stderr, proc.returncode
        except Exception as exc:  # noqa: BLE001
            return "", repr(exc), 1

    def handle_shutdown(self, req_id, params):
        log("shutdown")
        if self.player:
            self.player.shutdown()
        self._reply(req_id, {"accepted": True})
        raise SystemExit(0)

    # -- main loop
    def run(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                log("bad frame ignored")
                continue
            method = frame.get("method")
            if method is None:
                self._on_response(frame)
                continue
            req_id = frame.get("id")
            params = frame.get("params") or {}
            try:
                if method == "extension/initialize" and req_id is not None:
                    self.handle_initialize(req_id, params)
                elif method == "extension/initialized":
                    self.handle_initialized()
                elif method == "extension/ui/action" and req_id is not None:
                    self.handle_ui_action(req_id, params)
                elif method == "extension/shutdown" and req_id is not None:
                    self.handle_shutdown(req_id, params)
                elif method == "extension/intercept" and req_id is not None:
                    self.handle_intercept(req_id, params)
                elif method == "extension/event":
                    self.handle_event(params)
                elif method == "extension/resources/changed":
                    pass
                elif req_id is not None:
                    self._reply_error(req_id, -32601, "unknown method: %s" % method)
            except SystemExit:
                raise
            except Exception as exc:  # noqa: BLE001
                log("handler error: %r" % exc)
                if req_id is not None:
                    self._reply_error(req_id, -32603, "internal: %r" % exc)
        log("stdin EOF -> exit")
        if self.player:
            self.player.shutdown()


def main():
    log("start pid=%d argv=%r cwd=%s PLUGIN_ROOT=%r HOME=%r" % (
        os.getpid(), sys.argv, os.getcwd(),
        os.environ.get("REASONIX_PLUGIN_ROOT"), os.environ.get("REASONIX_HOME")))
    try:
        Sidecar().run()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log("fatal: %r" % exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
