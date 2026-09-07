from __future__ import annotations

import logging
import os
import queue
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

import multiprocessing as mp


_READER_DEBUG = str(os.environ.get("JARAM_LOG_READER_DEBUG", "")).strip().lower() in {
    "1", "true", "yes", "on",
}
_READER_LOGGER = logging.getLogger("jaram.log_reader.process")


def _multiscope_worker_main(cmd_q: mp.Queue, out_q: mp.Queue) -> None:
    usernames_by_uid: Dict[str, str] = {}
    cookies_by_uid: Dict[str, str] = {}
    server_label_by_uid: Dict[str, str] = {}
    ps_link_by_uid: Dict[str, str] = {}
    owner_by_uid: Dict[str, str] = {}

    engine = None
    last_reader_health = ""
    last_snapshot_signature = None
    pending_update_users: Optional[List[str]] = None
    pending_configure_webhooks: Optional[dict] = None
    pending_record_ocr_merchants: List[tuple[str, str]] = []

    def _send(msg: dict) -> None:
        try:
            out_q.put(msg)
        except Exception:
            pass

    def _log(msg: object) -> None:
        try:
            _send({"type": "log", "message": str(msg)})
        except Exception:
            pass

    try:
        from multiscope import MultiScopeEngine
    except Exception as e:
        _send({"type": "error", "message": f"Failed to import multiscope engine: {e!r}"})
        return

    def _get_username(uid: str) -> str:
        try:
            return str(usernames_by_uid.get(str(uid), "") or "")
        except Exception:
            return ""

    def _get_cookie(uid: str) -> str:
        try:
            return str(cookies_by_uid.get(str(uid), "") or "")
        except Exception:
            return ""

    def _get_server_label(uid: str) -> str:
        try:
            return str(server_label_by_uid.get(str(uid), "") or "")
        except Exception:
            return ""

    def _get_ps_link(uid: str) -> str:
        try:
            return str(ps_link_by_uid.get(str(uid), "") or "")
        except Exception:
            return ""

    def _get_owner(uid: str) -> str:
        try:
            return str(owner_by_uid.get(str(uid), "") or "")
        except Exception:
            return ""

    def _snapshot_signature(rows: List[dict]) -> tuple:
        signature = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            menu_by_uid = row.get("in_menu_by_uid") or {}
            menu_ts_by_uid = row.get("last_menu_ts_by_uid") or {}
            signature.append(
                (
                    str(row.get("server_key") or row.get("server") or ""),
                    tuple(str(uid) for uid in (row.get("users") or [])),
                    row.get("in_menu"),
                    float(row.get("last_menu_ts", 0.0) or 0.0),
                    tuple(sorted((str(uid), value) for uid, value in menu_by_uid.items())),
                    tuple(sorted((str(uid), float(value or 0.0)) for uid, value in menu_ts_by_uid.items())),
                    str(row.get("last_biome") or row.get("biome") or ""),
                    str(row.get("last_merchant") or row.get("merchant") or ""),
                    int(row.get("events", 0) or 0),
                )
            )
        return tuple(signature)

    def _publish_snapshot(*, force: bool = False) -> List[dict]:
        nonlocal last_snapshot_signature
        if engine is None:
            return []
        rows = engine.snapshot()
        signature = _snapshot_signature(rows)
        if force or signature != last_snapshot_signature:
            last_snapshot_signature = signature
            _send({"type": "snapshot", "rows": rows})
        return rows

    def _poll_engine() -> bool:
        nonlocal last_reader_health
        if engine is None:
            return False
        try:
            diagnostics = engine.poll_logs()
            events = engine.drain_events()
            if events:
                _send({"type": "events", "events": events})
            health = str(((diagnostics or {}).get("index") or {}).get("health") or "")
            if health and health != last_reader_health:
                last_reader_health = health
                _send({"type": "reader_health", "health": health, "diagnostics": diagnostics})
                if _READER_DEBUG:
                    _READER_LOGGER.debug("reader health=%s diagnostics=%r", health, diagnostics)
            _publish_snapshot()
            return bool(int((diagnostics or {}).get("backlog_users", 0) or 0))
        except Exception as exc:
            if _READER_DEBUG:
                _READER_LOGGER.debug("autonomous log poll failed", exc_info=exc)
            return False

    while True:
        backlog_pending = _poll_engine()
        try:
            # Drain existing byte backlogs without adding 100 ms between bounded
            # batches. Retain a 10 ms floor so a persistently unreadable file
            # cannot turn the child process into a hot loop.
            cmd = cmd_q.get(timeout=0.01 if backlog_pending else 0.1)
        except queue.Empty:
            continue
        except Exception:
            continue

        if not isinstance(cmd, dict):
            continue

        ctype = cmd.get("type")
        try:
            if ctype == "init":
                stats_path = cmd.get("stats_path")
                usernames_by_uid = {
                    str(k): str(v or "") for k, v in (cmd.get("usernames") or {}).items()
                }
                cookies_by_uid = {
                    str(k): str(v or "") for k, v in (cmd.get("cookies") or {}).items()
                }
                server_label_by_uid.clear()
                ps_link_by_uid.clear()
                owner_by_uid.clear()

                engine = MultiScopeEngine(
                    get_username=_get_username,
                    get_server_label=_get_server_label,
                    get_ps_link_for_user=_get_ps_link,
                    get_server_owner_for_user=_get_owner,
                    get_cookie_for_user=_get_cookie,
                    stats_path=str(stats_path) if stats_path else None,
                    log_fn=_log,
                )
                _send({"type": "ready"})

                # Apply any commands that arrived before init completed.
                try:
                    if pending_configure_webhooks:
                        engine.configure_webhooks(
                            biome_webhooks=pending_configure_webhooks.get("biome_webhooks") or [],
                            merchant_hook=str(pending_configure_webhooks.get("merchant_hook") or ""),
                            enable_jester=bool(pending_configure_webhooks.get("enable_jester", True)),
                            enable_mari=bool(pending_configure_webhooks.get("enable_mari", True)),
                            enable_rin=bool(pending_configure_webhooks.get("enable_rin", True)),
                            jester_ping=str(pending_configure_webhooks.get("jester_ping") or ""),
                            mari_ping=str(pending_configure_webhooks.get("mari_ping") or ""),
                            rin_ping=str(pending_configure_webhooks.get("rin_ping") or ""),
                            merchant_detection_mode=str(
                                pending_configure_webhooks.get("merchant_detection_mode") or "asset_id"
                            ),
                            disable_log_based_merchant_detection=bool(
                                pending_configure_webhooks.get("disable_log_based_merchant_detection", False)
                            ),
                            merchant_rate_limit=float(pending_configure_webhooks.get("merchant_rate_limit", 15.0) or 15.0),
                            biome_min_interval=float(pending_configure_webhooks.get("biome_min_interval", 2.0) or 2.0),
                            biome_modes=pending_configure_webhooks.get("biome_modes"),
                            skip_webhook_unknown_context=bool(
                                pending_configure_webhooks.get("skip_webhook_unknown_context", False)
                            ),
                            in_menu_none_timeout_seconds=float(
                                pending_configure_webhooks.get("in_menu_none_timeout_seconds", 120.0)
                                or 120.0
                            ),
                        )
                except Exception:
                    pass

                try:
                    if pending_update_users:
                        engine.update_users([str(u) for u in pending_update_users])
                        _publish_snapshot(force=True)
                except Exception:
                    pass
                try:
                    if pending_record_ocr_merchants:
                        for uid, merchant in pending_record_ocr_merchants:
                            engine.record_ocr_merchant(str(uid or ""), str(merchant or ""))
                        pending_record_ocr_merchants.clear()
                except Exception:
                    pass

            elif ctype == "update_users":
                if engine is None:
                    user_ids = cmd.get("user_ids") or []
                    if not isinstance(user_ids, list):
                        user_ids = list(user_ids)
                    pending_update_users = [str(u) for u in user_ids]
                    continue
                user_ids = cmd.get("user_ids") or []
                if not isinstance(user_ids, list):
                    user_ids = list(user_ids)
                user_ids = [str(u) for u in user_ids]
                user_ids_set = set(user_ids)
                incoming_usernames = cmd.get("usernames")
                incoming_cookies = cmd.get("cookies")
                refresh_user_ids: set[str] = set()
                if "usernames" in cmd and isinstance(incoming_usernames, dict):
                    normalized_usernames = {
                        str(k): str(v or "")
                        for k, v in incoming_usernames.items()
                        if str(k) in user_ids_set
                    }
                    refresh_user_ids = {
                        uid
                        for uid, username in normalized_usernames.items()
                        if uid in usernames_by_uid and usernames_by_uid.get(uid) != username
                    }
                    usernames_by_uid.clear()
                    usernames_by_uid.update(normalized_usernames)
                else:
                    for uid in list(usernames_by_uid.keys()):
                        if uid not in user_ids_set:
                            usernames_by_uid.pop(uid, None)
                if "cookies" in cmd and isinstance(incoming_cookies, dict):
                    cookies_by_uid.clear()
                    cookies_by_uid.update(
                        {
                            str(k): str(v or "")
                            for k, v in incoming_cookies.items()
                            if str(k) in user_ids_set
                        }
                    )
                else:
                    for uid in list(cookies_by_uid.keys()):
                        if uid not in user_ids_set:
                            cookies_by_uid.pop(uid, None)
                for uid in list(server_label_by_uid.keys()):
                    if uid not in user_ids_set:
                        server_label_by_uid.pop(uid, None)
                for uid in list(ps_link_by_uid.keys()):
                    if uid not in user_ids_set:
                        ps_link_by_uid.pop(uid, None)
                for uid in list(owner_by_uid.keys()):
                    if uid not in user_ids_set:
                        owner_by_uid.pop(uid, None)
                engine.update_users(user_ids, refresh_user_ids=refresh_user_ids)
                _publish_snapshot(force=True)

            elif ctype == "configure_webhooks":
                if engine is None:
                    pending_configure_webhooks = dict(cmd)
                    continue
                engine.configure_webhooks(
                    biome_webhooks=cmd.get("biome_webhooks") or [],
                    merchant_hook=str(cmd.get("merchant_hook") or ""),
                    enable_jester=bool(cmd.get("enable_jester", True)),
                    enable_mari=bool(cmd.get("enable_mari", True)),
                    enable_rin=bool(cmd.get("enable_rin", True)),
                    jester_ping=str(cmd.get("jester_ping") or ""),
                    mari_ping=str(cmd.get("mari_ping") or ""),
                    rin_ping=str(cmd.get("rin_ping") or ""),
                    merchant_detection_mode=str(cmd.get("merchant_detection_mode") or "asset_id"),
                    disable_log_based_merchant_detection=bool(
                        cmd.get("disable_log_based_merchant_detection", False)
                    ),
                    merchant_rate_limit=float(cmd.get("merchant_rate_limit", 15.0) or 15.0),
                    biome_min_interval=float(cmd.get("biome_min_interval", 2.0) or 2.0),
                    biome_modes=cmd.get("biome_modes"),
                    skip_webhook_unknown_context=bool(cmd.get("skip_webhook_unknown_context", False)),
                    in_menu_none_timeout_seconds=float(
                        cmd.get("in_menu_none_timeout_seconds", 120.0) or 120.0
                    ),
                )

            elif ctype == "recover_user_log_tracking":
                if engine is None:
                    continue
                recovered = engine.recover_user_log_tracking(
                    str(cmd.get("uid") or ""),
                    process_created_at=cmd.get("process_created_at"),
                )
                if recovered:
                    _publish_snapshot(force=True)

            elif ctype == "import_state":
                if engine is None:
                    continue
                state = cmd.get("state") or {}
                if isinstance(state, dict) and engine.import_state(state):
                    _publish_snapshot(force=True)

            elif ctype == "begin_handoff":
                if engine is None:
                    continue
                engine.begin_handoff(str(cmd.get("donor_uid") or ""), str(cmd.get("spare_uid") or ""))

            elif ctype == "complete_handoff":
                if engine is None:
                    continue
                engine.complete_handoff(str(cmd.get("donor_uid") or ""))

            elif ctype == "record_ocr_merchant":
                if engine is None:
                    pending_record_ocr_merchants.append(
                        (str(cmd.get("uid") or ""), str(cmd.get("merchant") or ""))
                    )
                    continue
                engine.record_ocr_merchant(
                    str(cmd.get("uid") or ""),
                    str(cmd.get("merchant") or ""),
                )

            elif ctype == "tick":
                if engine is None:
                    continue
                status_by_uid = cmd.get("status") or {}
                if isinstance(status_by_uid, dict):
                    for uid, st in status_by_uid.items():
                        u = str(uid)
                        if not isinstance(st, dict):
                            continue
                        server_label_by_uid[u] = str(st.get("server") or "")
                        ps_link_by_uid[u] = str(st.get("ps_link") or "")
                        owner_by_uid[u] = str(st.get("server_owner") or "")
                        if "username" in st:
                            usernames_by_uid[u] = str(st.get("username") or usernames_by_uid.get(u, ""))
                        if "cookie" in st:
                            cookies_by_uid[u] = str(st.get("cookie") or cookies_by_uid.get(u, ""))

                engine.tick(status_by_uid)
                rows = engine.snapshot()
                last_snapshot_signature = _snapshot_signature(rows)
                # A tick reply is also the command acknowledgement used by the
                # proxy to release its in-flight guard, so it must always send.
                _send({"type": "tick", "rows": rows, "events": engine.drain_events()})

            elif ctype == "rpc":
                rid = cmd.get("id")
                method = str(cmd.get("method") or "")
                args = cmd.get("args") or []
                kwargs = cmd.get("kwargs") or {}
                ok = True
                result: Any = None
                err = ""
                try:
                    if engine is None:
                        raise RuntimeError("MultiScope process not initialized")
                    fn = getattr(engine, method, None)
                    if fn is None:
                        raise AttributeError(f"MultiScopeEngine has no method '{method}'")
                    if not isinstance(args, list):
                        args = list(args)
                    if not isinstance(kwargs, dict):
                        kwargs = {}
                    result = fn(*args, **kwargs)
                except Exception as e:
                    ok = False
                    err = str(e)
                _send({"type": "rpc", "id": rid, "ok": ok, "result": result, "error": err})

            elif ctype == "shutdown":
                try:
                    if engine is not None:
                        engine.shutdown()
                except Exception:
                    pass
                return

        except Exception as e:
            _send({"type": "error", "message": f"{e!r}", "traceback": traceback.format_exc()})


class MultiScopeProcessProxy:
    """
    Runs MultiScopeEngine in a dedicated child process to avoid slowing the main worker loop.
    Public API mirrors the in-process engine methods used by gui.py / found_stats.py.
    """

    def __init__(
        self,
        *,
        usernames_by_uid: Dict[str, str],
        cookies_by_uid: Dict[str, str],
        stats_path: str,
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self._log = log_fn or (lambda _msg: None)
        self._ctx = mp.get_context("spawn")
        self._cmd_q: mp.Queue = self._ctx.Queue()
        self._out_q: mp.Queue = self._ctx.Queue()
        self._proc = self._ctx.Process(target=_multiscope_worker_main, args=(self._cmd_q, self._out_q), daemon=True)

        self._state_lock = threading.Lock()
        self._ready = False
        self._last_snapshot: List[dict] = []
        self._events: List[tuple[str, str, str]] = []
        self._reader_health: str = "degraded"
        self._reader_diagnostics: dict = {}
        self._rpc_replies: Dict[int, dict] = {}
        self._next_rpc_id = 1
        self._tick_pending = False
        self._tick_sent_ts = 0.0

        self._proc.start()
        self._send_cmd(
            {
                "type": "init",
                "stats_path": stats_path,
                "usernames": dict(usernames_by_uid or {}),
                "cookies": dict(cookies_by_uid or {}),
            }
        )

    def _send_cmd(self, msg: dict) -> None:
        try:
            if self._proc.is_alive():
                self._cmd_q.put(msg)
        except Exception:
            pass

    def _drain_out_queue(self, *, max_items: int = 200) -> None:
        logs: List[str] = []
        for _ in range(max_items):
            try:
                msg = self._out_q.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break

            if not isinstance(msg, dict):
                continue

            mtype = msg.get("type")
            if mtype == "ready":
                with self._state_lock:
                    self._ready = True
            elif mtype == "log":
                try:
                    logs.append(str(msg.get("message") or ""))
                except Exception:
                    continue
            elif mtype == "tick":
                rows = msg.get("rows") or []
                evs = msg.get("events") or []
                with self._state_lock:
                    self._tick_pending = False
                    if isinstance(rows, list):
                        self._last_snapshot = rows
                    if isinstance(evs, list):
                        for ev in evs:
                            try:
                                kind, uid, payload = ev
                                self._events.append((str(kind), str(uid), str(payload)))
                            except Exception:
                                continue
            elif mtype == "snapshot":
                rows = msg.get("rows") or []
                if isinstance(rows, list):
                    with self._state_lock:
                        self._last_snapshot = rows
            elif mtype == "events":
                evs = msg.get("events") or []
                if isinstance(evs, list):
                    with self._state_lock:
                        for ev in evs:
                            try:
                                kind, uid, payload = ev
                                self._events.append((str(kind), str(uid), str(payload)))
                            except Exception:
                                continue
            elif mtype == "reader_health":
                with self._state_lock:
                    self._reader_health = str(msg.get("health") or "degraded")
                    diag = msg.get("diagnostics")
                    self._reader_diagnostics = dict(diag) if isinstance(diag, dict) else {}
            elif mtype == "rpc":
                rid = msg.get("id")
                try:
                    rid_i = int(rid)
                except Exception:
                    continue
                with self._state_lock:
                    self._rpc_replies[rid_i] = msg
            elif mtype == "error":
                try:
                    logs.append(f"[MultiscopeProcess] {msg.get('message')}")
                    tb = msg.get("traceback")
                    if tb:
                        logs.append(str(tb))
                except Exception:
                    continue

        for line in logs:
            if line:
                try:
                    self._log(line)
                except Exception:
                    pass

    def _ensure_ready(self) -> bool:
        self._drain_out_queue()
        with self._state_lock:
            return bool(self._ready and self._proc.is_alive())

    def wait_ready(self, timeout_s: float = 2.0) -> bool:
        deadline = time.time() + float(timeout_s or 0.0)
        while time.time() < deadline:
            if self._ensure_ready():
                return True
            time.sleep(0.01)
        return self._ensure_ready()

    def update_users(
        self,
        user_ids: List[str],
        usernames_by_uid: Optional[Dict[str, str]] = None,
        cookies_by_uid: Optional[Dict[str, str]] = None,
    ) -> None:
        self._drain_out_queue()
        msg = {"type": "update_users", "user_ids": list(user_ids or [])}
        if usernames_by_uid is not None:
            msg["usernames"] = dict(usernames_by_uid or {})
        if cookies_by_uid is not None:
            msg["cookies"] = dict(cookies_by_uid or {})
        self._send_cmd(msg)

    def configure_webhooks(
        self,
        *,
        biome_webhooks: List[dict],
        merchant_hook: str = "",
        enable_jester: bool = True,
        enable_mari: bool = True,
        enable_rin: bool = True,
        jester_ping: str = "",
        mari_ping: str = "",
        rin_ping: str = "",
        merchant_detection_mode: str = "asset_id",
        disable_log_based_merchant_detection: bool = False,
        merchant_rate_limit: float = 15.0,
        biome_min_interval: float = 2.0,
        biome_modes: Optional[Dict[str, str]] = None,
        skip_webhook_unknown_context: bool = False,
        in_menu_none_timeout_seconds: float = 120.0,
    ) -> None:
        self._drain_out_queue()
        self._send_cmd(
            {
                "type": "configure_webhooks",
                "biome_webhooks": biome_webhooks or [],
                "merchant_hook": merchant_hook or "",
                "enable_jester": bool(enable_jester),
                "enable_mari": bool(enable_mari),
                "enable_rin": bool(enable_rin),
                "jester_ping": jester_ping or "",
                "mari_ping": mari_ping or "",
                "rin_ping": rin_ping or "",
                "merchant_detection_mode": str(merchant_detection_mode or "asset_id"),
                "disable_log_based_merchant_detection": bool(disable_log_based_merchant_detection),
                "merchant_rate_limit": float(merchant_rate_limit or 0.0),
                "biome_min_interval": float(biome_min_interval or 0.0),
                "biome_modes": biome_modes,
                "skip_webhook_unknown_context": bool(skip_webhook_unknown_context),
                "in_menu_none_timeout_seconds": float(in_menu_none_timeout_seconds),
            }
        )

    def begin_handoff(self, donor_uid: str, spare_uid: str) -> None:
        self._drain_out_queue()
        self._send_cmd({"type": "begin_handoff", "donor_uid": donor_uid, "spare_uid": spare_uid})

    def complete_handoff(self, donor_uid: str) -> None:
        self._drain_out_queue()
        self._send_cmd({"type": "complete_handoff", "donor_uid": donor_uid})

    def record_ocr_merchant(self, uid: str, merchant: str) -> None:
        self._drain_out_queue()
        uid_s = str(uid or "").strip()
        merchant_s = str(merchant or "").strip()
        if not uid_s or not merchant_s:
            return
        self._send_cmd({"type": "record_ocr_merchant", "uid": uid_s, "merchant": merchant_s})

    def tick(self, status_by_uid: Dict[str, dict]) -> None:
        self._drain_out_queue()
        if not self._ensure_ready():
            return

        now = time.time()
        with self._state_lock:
            pending = bool(self._tick_pending)
            last_sent = float(self._tick_sent_ts or 0.0)

        if pending and (now - last_sent) < 2.0:
            return

        # If the worker got stuck, allow a new tick after a timeout.
        with self._state_lock:
            self._tick_pending = True
            self._tick_sent_ts = now

        self._send_cmd({"type": "tick", "status": status_by_uid or {}})

    def snapshot(self) -> List[dict]:
        self._drain_out_queue()
        with self._state_lock:
            return list(self._last_snapshot or [])

    def drain_events(self) -> List[tuple[str, str, str]]:
        self._drain_out_queue()
        with self._state_lock:
            ev = list(self._events)
            self._events.clear()
        return ev

    def reader_health(self) -> str:
        self._drain_out_queue()
        with self._state_lock:
            return str(self._reader_health or "degraded")

    def reader_diagnostics(self) -> dict:
        self._drain_out_queue()
        with self._state_lock:
            return dict(self._reader_diagnostics or {})

    def diagnostics_snapshot(self) -> dict:
        return self.reader_diagnostics()

    def _rpc(self, method: str, *args: Any, timeout_s: float = 0.25, **kwargs: Any) -> Any:
        self._drain_out_queue()
        if not self._ensure_ready():
            raise RuntimeError("MultiScope process not ready")

        with self._state_lock:
            rid = int(self._next_rpc_id)
            self._next_rpc_id += 1

        self._send_cmd({"type": "rpc", "id": rid, "method": method, "args": list(args), "kwargs": dict(kwargs)})
        deadline = time.time() + float(timeout_s or 0.0)

        while time.time() < deadline:
            self._drain_out_queue()
            with self._state_lock:
                reply = self._rpc_replies.pop(rid, None)
            if reply is not None:
                if bool(reply.get("ok", False)):
                    return reply.get("result")
                raise RuntimeError(str(reply.get("error") or "RPC failed"))
            time.sleep(0.01)

        raise TimeoutError(f"MultiScope RPC timeout: {method}")

    def get_found_stats_snapshot(self) -> dict:
        out = self._rpc("get_found_stats_snapshot", timeout_s=0.25)
        return out if isinstance(out, dict) else {}

    def get_biomes_found_counts(self, window_seconds: float) -> dict:
        out = self._rpc("get_biomes_found_counts", window_seconds, timeout_s=0.25)
        return out if isinstance(out, dict) else {"counts": {}, "total": 0, "window_seconds": window_seconds}

    def get_merchants_found_counts(self, window_seconds: float) -> dict:
        out = self._rpc("get_merchants_found_counts", window_seconds, timeout_s=0.25)
        return out if isinstance(out, dict) else {"counts": {}, "total": 0, "window_seconds": window_seconds}

    def recover_user_log_tracking(
        self,
        uid: str,
        process_created_at: Optional[float] = None,
    ) -> bool:
        uid_s = str(uid or "").strip()
        if not uid_s:
            return False
        try:
            out = self._rpc(
                "recover_user_log_tracking",
                uid_s,
                process_created_at=process_created_at,
                timeout_s=1.0,
            )
            return bool(out)
        except Exception:
            return False

    def recover_user_log_tracking_async(
        self,
        uid: str,
        process_created_at: Optional[float] = None,
    ) -> bool:
        """Queue recovery without stalling the manager heartbeat on an RPC."""
        uid_s = str(uid or "").strip()
        if not uid_s:
            return False
        self._drain_out_queue()
        try:
            if not self._proc.is_alive():
                return False
        except Exception:
            return False
        self._send_cmd(
            {
                "type": "recover_user_log_tracking",
                "uid": uid_s,
                "process_created_at": process_created_at,
            }
        )
        return True

    def export_state(self) -> dict:
        # Pause/Resume needs this to succeed even if the worker is busy processing a tick;
        # allow a longer timeout so state isn't silently lost and values don't reset on resume.
        if not self.wait_ready(timeout_s=10.0):
            return {}
        try:
            out = self._rpc("export_state", timeout_s=10.0)
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}

    def import_state(self, state: dict) -> bool:
        # Same rationale as export_state: tolerate slower process startup and busy ticks.
        if not self.wait_ready(timeout_s=10.0):
            return False
        try:
            out = self._rpc("import_state", state, timeout_s=10.0)
            return bool(out)
        except Exception:
            return False

    def import_state_async(self, state: dict) -> bool:
        """Queue a resume import so manager readiness does not wait on log I/O."""
        if not isinstance(state, dict) or not state:
            return False
        self._drain_out_queue()
        try:
            if not self._proc.is_alive():
                return False
        except Exception:
            return False
        self._send_cmd({"type": "import_state", "state": state})
        return True

    def shutdown(self) -> None:
        self._drain_out_queue()
        try:
            self._send_cmd({"type": "shutdown"})
        except Exception:
            pass

        try:
            self._proc.join(timeout=2.0)
        except Exception:
            pass
        try:
            if self._proc.is_alive():
                self._proc.terminate()
        except Exception:
            pass
