import psutil
import os
import time
import threading
import win32gui
import win32process
import requests
import json
from log_utils import LogLookupResult, PreconnectTracker, find_log_match
from pathlib import Path
from launch_priority import (
    consume_launch_last_once,
    launch_queue_sort_key,
    mark_launch_last_once,
    sort_user_items_by_launch_priority,
)
from cap_watchdog import (
    DEFAULT_CAP_WATCHDOG_SETTINGS,
    increment_cap_counter,
    normalize_cap_watchdog_settings,
)
from merchant_fix import get_merchant_fix_controller

try:
    from roblox_cookie_utils import (
        extract_roblosecurity_from_requests_response,
        normalize_roblosecurity_cookie_value,
        persist_updated_cookie,
    )
except Exception:
    extract_roblosecurity_from_requests_response = None
    normalize_roblosecurity_cookie_value = None
    persist_updated_cookie = None

try:
    from gui import ConfigManager
except ImportError:


    
    def limit_strap_helpers(threshold: int = 50, *, kill_all: bool = False) -> None:
        """
        Trim *-strap.exe* helpers.

        • kill_all = False  ➜ keep the **oldest** helper and terminate any
        extras once the running count reaches or exceeds *threshold*.
        • kill_all = True   ➜ terminate **every** helper.

        Pass threshold=1 to “kill all but oldest” unconditionally.
        """
        helpers = [
            p for p in psutil.process_iter(['name', 'create_time'])
            if (n := p.info['name']) and n.lower().endswith('strap.exe')
        ]
        if not helpers:
            return

        if kill_all:
            for p in helpers:
                try:
                    p.kill()
                except Exception:
                    pass
            return

        if len(helpers) < threshold:
            return                                    # nothing to trim

        helpers.sort(key=lambda p: p.info['create_time'])  # oldest first
        for p in helpers[1:]:                         # keep index-0
            try:
                p.kill()
            except Exception:
                pass


    def limit_roblox_crash_handlers(threshold: int = 2, *, kill_all: bool = False) -> None:
        """
        Trim RobloxCrashHandler*.exe processes.

        ƒ?› kill_all = False  ƒzo keep the **oldest** crash handler and terminate any
        extras once the running count reaches or exceeds *threshold*.
        ƒ?› kill_all = True   ƒzo terminate **every** crash handler.

        Pass threshold=2 to keep at most one; threshold=1 to trim unconditionally.
        """
        crash_handlers = []
        try:
            for p in psutil.process_iter(['name', 'create_time']):
                try:
                    name = p.info.get('name')
                    if not name:
                        continue
                    n = str(name).lower()
                    if n.startswith('robloxcrashhandler') and n.endswith('.exe'):
                        crash_handlers.append(p)
                except Exception:
                    continue
        except Exception:
            return

        if not crash_handlers:
            return

        if kill_all:
            for p in crash_handlers:
                try:
                    p.kill()
                except Exception:
                    pass
            return

        if len(crash_handlers) < threshold:
            return

        crash_handlers.sort(key=lambda p: p.info.get('create_time') or 0)  # oldest first
        for p in crash_handlers[1:]:  # keep index-0
            try:
                p.kill()
            except Exception:
                pass


    def limit_msedgewebview2_processes(threshold: int = 1, *, kill_all: bool = True) -> None:
        """
        Trim msedgewebview2.exe processes.

        - kill_all = True (default): terminate every msedgewebview2.exe process.
        - kill_all = False: keep the oldest process and terminate extras once the
          running count reaches or exceeds *threshold*.
        """
        webviews = []
        try:
            for p in psutil.process_iter(['name', 'create_time']):
                try:
                    name = p.info.get('name')
                    if name and str(name).lower() == 'msedgewebview2.exe':
                        webviews.append(p)
                except Exception:
                    continue
        except Exception:
            return

        if not webviews:
            return

        if kill_all:
            for p in webviews:
                try:
                    p.kill()
                except Exception:
                    pass
            return

        if len(webviews) < threshold:
            return

        webviews.sort(key=lambda p: p.info.get('create_time') or 0)  # oldest first
        for p in webviews[1:]:  # keep index-0
            try:
                p.kill()
            except Exception:
                pass


    class ConfigManager:
        def __init__(self):
            self.app_name = "JARAM"
            self.config_dir = self._get_config_directory()
            self.users_file = self.config_dir / "users.json"
            self.settings_file = self.config_dir / "settings.json"
            self.default_settings = {
                "window_limit": 1,
                "timeouts": {
                    "offline": 25,
                    "launch_delay": 10,
                    "initial_delay": 10,
                    "kill_timeout": 1740,
                    "poll_interval": 10,
                },
                "cap_watchdog": dict(DEFAULT_CAP_WATCHDOG_SETTINGS),
                "merchant_fix": {
                    "smart_enabled": False,
                    "smart_block_trigger": "boss_raid_ui",
                    "post_marker_delay_seconds": 0.0,
                    "smart_user_scope": "blacklist",
                    "smart_user_ids": [],
                },
            }
            self._ensure_directories()

        def _get_config_directory(self):
            if os.name == 'nt':
                appdata = os.environ.get('APPDATA')
                if appdata:
                    return Path(appdata) / self.app_name
            return Path.home() / f".{self.app_name.lower()}"

        def _ensure_directories(self):
            try:
                self.config_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                pass
        # ── new ─────────────────────────────────────────────
        def _deep_update(self, base: dict, updates: dict):
            """Recursive dict.update so nested keys survive partial files."""
            for k, v in updates.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    base[k] = self._deep_update(base[k], v)
                else:
                    base[k] = v
            return base

        def load_settings(self):
            try:
                if self.settings_file.exists():
                    with open(self.settings_file, 'r', encoding='utf-8') as f:
                        loaded = json.load(f)

                    # start from defaults, then deep-merge file content
                    settings = json.loads(json.dumps(self.default_settings))  # deep copy
                    settings = self._deep_update(settings, loaded)
                    return settings
                else:
                    return json.loads(json.dumps(self.default_settings))
            except Exception:
                return json.loads(json.dumps(self.default_settings))

# ──────────────────────────────────────────────────────────────
# 1-A. RobloxManager – strip presence monitor & shorter loop
# ──────────────────────────────────────────────────────────────
class RobloxManager:
    def __init__(self, config_manager: "ConfigManager" = None):
        # use the GUI’s instance if one is provided
        self.config_manager = config_manager or ConfigManager()
        self.settings        = self._load_settings()
        self.process_tracker = ProcessTracker()
        self.auth_handler    = AuthenticationHandler()

        # ⬇ delete: self.presence_monitor = PresenceMonitor()

        app_settings = self._load_app_settings()
        self.target_place = "15532962292"
        self.window_limit = app_settings.get("window_limit", 1)

        # presence key removed, default tick every 2 s
        self.check_intervals = {
            'window'   : 3,
            'cleanup'  : 30,
            'main_tick': 2
        }

        timeouts = app_settings.get("timeouts", {})

        self.timeouts = {
            "relaunch"     : 20,
            "launch_delay" : timeouts.get("launch_delay", 4),
            "offline"      : timeouts.get("offline",      35),
            "initial_delay": timeouts.get("initial_delay",4)
        }

        self.excluded_pid = 0
        from timeout_monitor import TimeoutMonitor   # top-level import

        tm_cfg = app_settings.get("timeout_monitor", {}) or {}
        if not isinstance(tm_cfg, dict):
            tm_cfg = {}
        alerts_cfg = app_settings.get("alerts", {}) or {}
        if not isinstance(alerts_cfg, dict):
            alerts_cfg = {}
        webhook_url = str(alerts_cfg.get("webhook_url") or tm_cfg.get("webhook_url") or "").strip()
        blackout_ping = str(
            alerts_cfg.get("blackout_ping")
            or alerts_cfg.get("ping_message")
            or tm_cfg.get(
                "ping_message",
                "<@YourPing> This message is sent whenever your active processes drop to 1 or 0, for debugging, leave webhook empty if not interested",
            )
            or ""
        ).strip()

        self.timeout_monitor = TimeoutMonitor(
            kill_timeout  = tm_cfg.get("kill_timeout", 1740),
            poll_interval = tm_cfg.get("poll_interval", 10),
            webhook_url   = webhook_url,
            ping_message  = blackout_ping,
            kill_enabled  = bool(tm_cfg.get("kill_enabled", True))
        )


    def _load_settings(self):
        try:

            if hasattr(self.config_manager, 'get_users_for_manager'):
                return self.config_manager.get_users_for_manager()   # keep ALL users

            elif not is_alternate:
                users = self.config_manager.load_users()

            if not users:
                return {}
            return users
        except Exception as error:
            return {}

    def _load_app_settings(self):
        try:
            if hasattr(self.config_manager, 'load_settings'):
                return self.config_manager.load_settings()
            else:

                return {
                    "window_limit": 1,
                    "timeouts": {
                        "offline": 35,
                        "launch_delay": 4
                    }
                }
        except Exception as error:
            return {
                "window_limit": 1,
                "timeouts": {
                    "offline": 35,
                    "launch_delay": 4
                }
            }

class ProcessTracker:
    def __init__(self):
        from collections import defaultdict
        self.user_processes = defaultdict(list)   # user_id -> [pids]
        self.process_owners = {}                  # pid -> user_id
        self.creation_timestamps = {}             # pid -> create_time
        self.user_server = {}                     # user_id -> human label of server joined
        # Per-user grace window used to avoid false "DISCONNECTED" during launch/PID handoff.
        self.pid_grace_until = {}                 # user_id -> epoch seconds
        self.protection_period = 60               # seconds to protect very new PIDs from aggression
        self.initialization_mode = False
    
        # NEW: per-user resolved private server OWNER username
        self.server_owner = {}                    # user_id -> owner username
        
        # NEW: cache the exact PS link code and place used at launch
        self.user_ps_code  = {}   # user_id -> full linkCode string
        self.user_ps_place = {}   # user_id -> placeId string
        # Tracks short-lived reservations so normal launches avoid in-flight handoffs
        self.reserved_servers = {}   # label -> {"by": uid, "type": "handoff"|"normal", "exp": epoch}
        # throttle normal-launch retries when a target server is occupied (per-user TTL)
        self.skip_until_by_user = {}   # uid -> epoch seconds
        # map share-code -> {"place": "...", "link": "..."} once any user resolves it
        self.share_to_link = {}   # e.g. {"A1B2C3D4": {"place": "15532962292", "link": "0669103657"}}




class AuthenticationHandler:
    def __init__(self):
        self.token_cache = {}

    def retrieve_csrf_token(self, cookie):
        cookie = str(cookie or "").strip()
        if normalize_roblosecurity_cookie_value is not None:
            try:
                cookie = normalize_roblosecurity_cookie_value(cookie)
            except Exception:
                cookie = str(cookie or "").strip()
        if cookie in self.token_cache and self.token_cache[cookie]["expires"] > time.time():
            return self.token_cache[cookie]["token"]

        session = requests.Session()
        session.cookies[".ROBLOSECURITY"] = cookie
        session.headers.update({
            "Referer": "https://www.roblox.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        })

        try:
            response = session.post("https://auth.roblox.com/v1/authentication-ticket", timeout=5)
            if response.status_code == 403 and "x-csrf-token" in response.headers:
                token = response.headers["x-csrf-token"]
                self.token_cache[cookie] = {
                    "token": token,
                    "expires": time.time() + 1800
                }
                return token
        except Exception as error:
            pass
        return None

    def obtain_auth_ticket(self, cookie):
        cookie = str(cookie or "").strip()
        if normalize_roblosecurity_cookie_value is not None:
            try:
                cookie = normalize_roblosecurity_cookie_value(cookie)
            except Exception:
                cookie = str(cookie or "").strip()
        session = requests.Session()
        auth_meta = {"mark_bad": False, "failure_reason": "no_response"}
        session.headers.update(
            {
                "Referer": "https://www.roblox.com/",
                "User-Agent": "Roblox/WinInet",
            }
        )
        try:
            session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com", path="/")
        except Exception:
            try:
                session.cookies[".ROBLOSECURITY"] = cookie
            except Exception:
                pass

        def _maybe_update_cookie(resp) -> None:
            nonlocal cookie
            if not resp or extract_roblosecurity_from_requests_response is None:
                return
            try:
                updated = extract_roblosecurity_from_requests_response(resp, session=session)
            except Exception:
                updated = None
            if updated and updated != cookie:
                cookie = updated
                try:
                    session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com", path="/")
                except Exception:
                    pass

        try:
            response = session.post("https://auth.roblox.com/v1/authentication-ticket", timeout=5)
        except Exception:
            return None, cookie, auth_meta

        _maybe_update_cookie(response)

        try:
            ticket = response.headers.get("rbx-authentication-ticket")
        except Exception:
            ticket = None
        if ticket:
            return ticket, cookie, {"mark_bad": False, "failure_reason": ""}

        if response.status_code == 403:
            csrf_token = response.headers.get("x-csrf-token") or response.headers.get("X-CSRF-TOKEN")
            if csrf_token:
                session.headers.update(
                    {
                        "X-CSRF-TOKEN": csrf_token,
                        "Content-Type": "application/json",
                    }
                )
                try:
                    response = session.post("https://auth.roblox.com/v1/authentication-ticket", timeout=5)
                except Exception:
                    return None, cookie, auth_meta
                _maybe_update_cookie(response)
                try:
                    ticket = response.headers.get("rbx-authentication-ticket")
                except Exception:
                    ticket = None
                if ticket:
                    return ticket, cookie, {"mark_bad": False, "failure_reason": ""}

        status_code = getattr(response, "status_code", None)
        auth_meta = {
            "mark_bad": status_code in (401, 403),
            "failure_reason": f"http_{status_code}" if status_code is not None else "no_response",
        }
        return None, cookie, auth_meta

# ──────────────────────────────────────────────────────────────
# 1-B. presence monitor class – delete the whole class
#     (PresenceMonitor … end)
# ──────────────────────────────────────────────────────────────

class ProcessManager:
    def __init__(self, excluded_pid=0):
        self.excluded_pid = excluded_pid
        self.process_name = "RobloxPlayerBeta.exe"

    def is_game_active(self):
        for process in psutil.process_iter(['name', 'pid']):
            if process.info['name'] == self.process_name and process.info['pid'] != self.excluded_pid:
                return True
        return False

    def terminate_process(self, pid=None, tracker=None):
        """
        Kill RobloxPlayerBeta.exe processes without calling taskkill.exe.

        • If pid is given: kill that PID only.
        • If pid is None: kill all matching processes (except excluded_pid).
        """
        def _cleanup_tracker(_pid: int):
            if not tracker:
                return
            # Remove from per-user lists
            user_id = tracker.process_owners.get(_pid)
            if user_id:
                lst = tracker.user_processes.get(user_id, [])
                if _pid in lst:
                    lst.remove(_pid)
            tracker.process_owners.pop(_pid, None)
            tracker.creation_timestamps.pop(_pid, None)

        def _kill_one(_pid: int) -> bool:
            if _pid == self.excluded_pid:
                return False
            try:
                proc = psutil.Process(_pid)
            except psutil.NoSuchProcess:
                _cleanup_tracker(_pid)
                return False

            try:
                # Try a graceful terminate first
                proc.terminate()
                try:
                    proc.wait(5)
                except psutil.TimeoutExpired:
                    # Force kill if it doesn't die in 5s
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                # Nothing more we can do
                pass

            _cleanup_tracker(_pid)
            return True

        # --- Single PID branch ---------------------------------------------
        if pid is not None:
            return _kill_one(pid)

        # --- Kill all RobloxPlayerBeta.exe processes -----------------------
        terminated_any = False
        try:
            for process in psutil.process_iter(['pid', 'name']):
                try:
                    name = process.info.get('name')
                    _pid = process.info.get('pid')
                except (psutil.NoSuchProcess,
                        psutil.AccessDenied,
                        psutil.ZombieProcess):
                    continue

                if name == self.process_name and _pid != self.excluded_pid:
                    if _kill_one(_pid):
                        terminated_any = True
        except (OSError, psutil.Error):
            # If the system is under resource pressure, just bail out gracefully.
            pass

        return terminated_any
    
    def count_windows_by_process(self):
        """
        Count visible top-level windows belonging to RobloxPlayerBeta.exe PIDs.

        Hardened so that:
          • psutil.process_iter() OSError/WinError 8 is swallowed gracefully
          • per-process info errors (AccessDenied, disappeared PIDs) are ignored
          • EnumWindows failures don't crash the worker loop
        """
        from collections import defaultdict

        window_counts = defaultdict(int)
        active_pids = set()

        # --- Safely collect Roblox PIDs ------------------------------------
        try:
            for process in psutil.process_iter(['pid', 'name']):
                try:
                    name = process.info.get('name')
                    pid  = process.info.get('pid')
                except (psutil.NoSuchProcess,
                        psutil.AccessDenied,
                        psutil.ZombieProcess):
                    continue

                if name == self.process_name and pid != self.excluded_pid:
                    active_pids.add(pid)
        except (OSError, psutil.Error):
            # WinError 8 / resource issues → just skip this pass
            return window_counts

        if not active_pids:
            return window_counts

        # --- Count windows for those PIDs ----------------------------------
        def window_callback(hwnd, extra):
            try:
                if win32gui.IsWindowVisible(hwnd):
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if pid in active_pids:
                        window_counts[pid] += 1
            except Exception:
                # Never let a bad window handle kill the loop
                pass

        try:
            win32gui.EnumWindows(window_callback, None)
        except Exception:
            # If EnumWindows itself fails, just return what we have
            pass

        return window_counts

    def verify_process_active(self, pid):
            try:
                process = psutil.Process(pid)
                return process.name() == self.process_name and pid != self.excluded_pid
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                return False
            except Exception:
                # Any unexpected psutil error => treat as inactive so the loop doesn't die
                return False


    def await_new_process(self, user_id, launch_timestamp, timeout, tracker):
        start_time = time.time()
        iters = 0
        last_err = None
        newer_candidates = []
        seen_candidate_pids = set()
        ignored_pids = set()

        stable_seconds = 3.0
        # Once we've seen a candidate process and it disappears, we enter a final
        # short handoff window where we will NOT fall back to the full `timeout`.
        handoff_deadline = None
        picked_pid = None
        picked_ct = None
        picked_at = None

        initial_deadline = start_time + float(timeout or 0)
        deadline = initial_deadline
        hard_deadline = initial_deadline + stable_seconds

        def _emit(msg: str) -> None:
            sink = getattr(tracker, "debug_log", None) if tracker is not None else None
            if not callable(sink):
                sink = print
            try:
                sink(msg)
            except Exception:
                try:
                    print(msg)
                except Exception:
                    pass

        while time.time() < deadline:
            iters += 1
            now = time.time()

            # If our currently picked PID vanished within the stabilization window, drop it.
            if picked_pid is not None:
                alive = False
                try:
                    alive = self.verify_process_active(picked_pid)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    if err != last_err:
                        _emit(f"[PIDWAIT ERROR] uid={user_id} err={err}")
                    last_err = err
                    alive = False

                if not alive:
                    age = (now - picked_at) if picked_at is not None else None
                    _emit(f"[PIDWAIT DROP] uid={user_id} pid={picked_pid} ct={picked_ct} age={age}")
                    ignored_pids.add(picked_pid)
                    picked_pid = None
                    picked_ct = None
                    picked_at = None

                    # Final handoff timeout: after the first disappearance, only give
                    # `stable_seconds` to find another suitable process.
                    if handoff_deadline is None:
                        handoff_deadline = now + stable_seconds
                        deadline = handoff_deadline
                        _emit(
                            f"[PIDWAIT HANDOFF] uid={user_id} final_deadline={handoff_deadline} "
                            f"dt={handoff_deadline - start_time}"
                        )

            # Scan for the newest candidate created after launch_timestamp.
            best_pid = None
            best_ct = None
            try:
                for process in psutil.process_iter(['pid', 'name', 'create_time']):
                    try:
                        info = process.info or {}
                        if info.get('name') != self.process_name:
                            continue

                        pid = info.get('pid')
                        if not pid or pid == self.excluded_pid:
                            continue

                        create_time = info.get('create_time')
                        if not create_time or create_time <= launch_timestamp:
                            continue

                        owned = False
                        try:
                            owned = pid in tracker.process_owners
                        except Exception:
                            owned = False

                        if pid not in seen_candidate_pids and len(newer_candidates) < 8:
                            seen_candidate_pids.add(pid)
                            newer_candidates.append((pid, create_time, bool(owned)))

                        if owned or pid in ignored_pids:
                            continue

                        if (
                            best_ct is None
                            or create_time > best_ct
                            or (create_time == best_ct and (best_pid is None or pid > best_pid))
                        ):
                            best_pid = pid
                            best_ct = create_time
                    except Exception as e:
                        err = f"{type(e).__name__}: {e}"
                        if err != last_err:
                            _emit(f"[PIDWAIT ERROR] uid={user_id} err={err}")
                        last_err = err
                        continue
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                if err != last_err:
                    _emit(f"[PIDWAIT ERROR] uid={user_id} err={err}")
                last_err = err

            # Always prefer the newest process. If a newer one appears, switch and restart the stability window.
            if best_pid is not None and (picked_ct is None or best_ct > picked_ct):
                picked_pid = best_pid
                picked_ct = best_ct
                picked_at = now
                # Only extend the deadline to allow stabilization while we're still
                # in the initial wait; once we enter the handoff window, its deadline
                # is final.
                if handoff_deadline is None:
                    try:
                        deadline = max(deadline, min(hard_deadline, picked_at + stable_seconds))
                    except Exception:
                        pass
                try:
                    grace_map = getattr(tracker, "pid_grace_until", None)
                    if grace_map is None:
                        tracker.pid_grace_until = grace_map = {}
                    grace_map[user_id] = picked_at + stable_seconds
                except Exception:
                    pass
                _emit(f"[PIDWAIT PICK] uid={user_id} pid={picked_pid} ct={picked_ct}")

            # If the current pick survives for stable_seconds, accept it as the launch PID.
            required_alive = stable_seconds if handoff_deadline is None else 0.0
            if picked_pid is not None and picked_at is not None and (now - picked_at) >= required_alive:
                stable_alive = False
                try:
                    stable_alive = self.verify_process_active(picked_pid)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    if err != last_err:
                        _emit(f"[PIDWAIT ERROR] uid={user_id} err={err}")
                    last_err = err
                    stable_alive = False

                if stable_alive:
                    try:
                        owned = picked_pid in tracker.process_owners
                    except Exception:
                        owned = False

                    if not owned:
                        try:
                            tracker.process_owners[picked_pid] = user_id
                        except Exception:
                            pass
                        try:
                            if picked_pid not in tracker.user_processes[user_id]:
                                tracker.user_processes[user_id].append(picked_pid)
                        except Exception:
                            pass
                    try:
                        tracker.creation_timestamps[picked_pid] = picked_ct
                    except Exception:
                        pass

                    _emit(
                        f"[PIDWAIT OK] uid={user_id} pid={picked_pid} ct={picked_ct} "
                        f"iters={iters} dt={now - start_time}"
                    )
                    return picked_pid

            time.sleep(0.5)

        _emit(
            f"[PIDWAIT TIMEOUT] uid={user_id} timeout={timeout} iters={iters} launch_ts={launch_timestamp} "
            f"newer_candidates={newer_candidates} last_err={last_err}"
        )
        return None

    def cleanup_dead_processes(self, tracker):
        def _emit(msg: str) -> None:
            sink = getattr(tracker, "debug_log", None) if tracker is not None else None
            if not callable(sink):
                sink = print
            try:
                sink(msg)
            except Exception:
                try:
                    print(msg)
                except Exception:
                    pass

        active_pids = set()
        for process in psutil.process_iter(['pid', 'name']):
            if process.info['name'] == self.process_name and process.info['pid'] != self.excluded_pid:
                active_pids.add(process.info['pid'])

        dead_pids = set(tracker.process_owners.keys()) - active_pids

        for pid in dead_pids:
            user_id = tracker.process_owners[pid]
            try:
                ct = tracker.creation_timestamps.get(pid)
            except Exception:
                ct = None
            _emit(f"[PID DEAD] pid={pid} uid={user_id} ct={ct}")
            if pid in tracker.user_processes.get(user_id, []):
                tracker.user_processes[user_id].remove(pid)
            del tracker.process_owners[pid]
            if pid in tracker.creation_timestamps:
                del tracker.creation_timestamps[pid]
        
        # NEW: move users with no live processes into the disconnected pool
        for uid, lst in list(tracker.user_processes.items()):
            if not lst:
                grace_until = 0
                try:
                    grace_until = (tracker.pid_grace_until or {}).get(uid, 0)
                except Exception:
                    grace_until = 0
                if grace_until and time.time() < grace_until:
                    continue
                tracker.user_server[uid] = "DISCONNECTED"

    def eliminate_orphaned_processes(self, tracker, valid_users):
        eliminated = False
        current_time = time.time()

        if tracker.initialization_mode:
            return False

        for process in psutil.process_iter(['pid', 'name', 'create_time']):
            if process.info['name'] == self.process_name and process.info['pid'] != self.excluded_pid:
                pid = process.info['pid']
                process_create_time = process.info['create_time']

                if current_time - process_create_time < tracker.protection_period:
                    continue

                if pid not in tracker.process_owners:
                    self.terminate_process(pid, tracker)
                    eliminated = True
                elif tracker.process_owners[pid] not in valid_users:
                    self.terminate_process(pid, tracker)
                    eliminated = True

        return eliminated

class GameLauncher:
    # Guards all GameLauncher instances in this Python process. The instance-level
    # guard below is not enough when a manual launcher, old ramp thread, or resumed
    # worker owns a different GameLauncher object.
    _global_launch_inflight_lock = threading.Lock()
    _global_launch_inflight = set()
    _global_launch_attempt_lock = threading.Lock()

    def __init__(self,
                 target_place,
                 process_mgr,
                 auth_handler,
                 process_tracker,
                 config_mgr,
                 launch_delay=4,
                 initial_delay=4,
                 log_fn=None):
        self.target_place     = target_place
        self.process_manager  = process_mgr
        self.auth_handler     = auth_handler
        self.tracker          = process_tracker
        self.cfg              = config_mgr

        self.launch_delay  = launch_delay
        self.initial_delay = initial_delay
        self.process_timeout = 20
        self.process_timeout = 20
        self.log = log_fn or print
        self._skip_log_until = {}  # (uid, label) -> epoch seconds

        # Prevent duplicate launches of the same uid from concurrent callers (ramp-up thread,
        # relaunch scheduler, disconnect handling, manual restarts, etc.).
        self._launch_inflight_lock = threading.Lock()
        self._launch_inflight = set()

    def _record_account_launch_activity(self, user_id, launched_at=None) -> None:
        marker = getattr(self.cfg, "mark_user_launched", None)
        if callable(marker):
            try:
                marker(str(user_id), launched_at if launched_at is not None else time.time())
            except Exception:
                pass


    def _extract_private_server_info(self, private_server_link, cookie=None):
        import re
        if not private_server_link:
            return None, None, "direct", cookie

        link = str(private_server_link or "").strip()

        pattern1 = r'roblox\.com/games/(\d+)/[^?]*\?privateServerLinkCode=([A-Za-z0-9_-]+)'
        m1 = re.search(pattern1, link)
        if m1:
            return m1.group(1), m1.group(2), "direct", cookie

        pattern2 = r'roblox\.com/share\?code=([A-Za-z0-9_-]+)&type=Server'
        m2 = re.search(pattern2, link)
        if m2:
            share_code = m2.group(1)
            if cookie:
                p, code, updated_cookie = self._convert_share_link(share_code, cookie)
                if updated_cookie:
                    cookie = updated_cookie
                if p and code:
                    return p, code, "resolved", cookie
                return None, share_code, "share", cookie
            return None, share_code, "share", cookie

        # Allow pasting just a linkCode value (privateServerLinkCode).
        if re.fullmatch(r"[A-Za-z0-9_-]{5,}", link):
            return None, link, "code", cookie

        return None, None, "invalid", cookie
    
    # main.py — inside class GameLauncher
    def log_skip(self, user_id: str, server_label: str, reason: str, throttle: float = 8.0) -> None:
        """
        Consistent, throttled [LAUNCH SKIP] logging usable from preflight checks.
        Keyed by (uid, label, reason) so you still see different reasons.
        """
        now = time.time()
        key = (user_id, server_label, reason)
        if self._skip_log_until.get(key, 0.0) <= now:
            self._skip_log_until[key] = now + float(throttle or 0)
            self.log(f"[LAUNCH SKIP] {user_id} -> {server_label} {reason}")


    def _related_window_search_pids(self, pid_or_pids):
        targets = set()
        try:
            if isinstance(pid_or_pids, (set, tuple, list)):
                for val in pid_or_pids:
                    try:
                        iv = int(val)
                        if iv > 0:
                            targets.add(iv)
                    except Exception:
                        continue
            else:
                iv = int(pid_or_pids)
                if iv > 0:
                    targets.add(iv)
        except Exception:
            pass

        roots = list(targets)
        for root_pid in roots:
            try:
                proc = psutil.Process(int(root_pid))
            except Exception:
                continue
            try:
                for child in proc.children(recursive=True):
                    try:
                        cpid = int(child.pid)
                        if cpid > 0:
                            targets.add(cpid)
                    except Exception:
                        continue
            except Exception:
                continue

        return targets

    def _find_visible_window_for_pid(self, pid_or_pids, *, include_hidden: bool = False, include_minimized: bool = False):
        targets = self._related_window_search_pids(pid_or_pids)
        if not targets:
            return None

        candidates = []

        def window_callback(hwnd, _extra):
            try:
                visible = bool(win32gui.IsWindowVisible(hwnd))
                iconic = bool(win32gui.IsIconic(hwnd))
                if not include_hidden and not visible:
                    return
                if not include_minimized and iconic:
                    return
                _, hwnd_pid = win32process.GetWindowThreadProcessId(hwnd)
                if int(hwnd_pid) not in targets:
                    return
                candidates.append((hwnd, visible, iconic))
            except Exception:
                pass

        try:
            win32gui.EnumWindows(window_callback, None)
        except Exception:
            pass

        if not candidates:
            return None

        best_hwnd = None
        best_key = None
        for hwnd, visible, iconic in candidates:
            try:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                w = max(0, int(r) - int(l))
                h = max(0, int(b) - int(t))
                area = int(w * h)
                # Prefer visible, non-minimized windows with largest area.
                key = (1 if visible else 0, 1 if not iconic else 0, area)
                if best_key is None or key > best_key:
                    best_key = key
                    best_hwnd = hwnd
            except Exception:
                continue
        return best_hwnd

    def _wait_for_visible_window_for_pid(self, pid_or_pids, timeout_s: float = 8.0):
        targets = self._related_window_search_pids(pid_or_pids)
        deadline = time.time() + float(timeout_s or 0)
        refresh_at = 0.0

        while time.time() < deadline:
            now = time.time()
            if now >= refresh_at:
                targets = self._related_window_search_pids(targets)
                refresh_at = now + 0.5

            hwnd = self._find_visible_window_for_pid(targets)
            if hwnd:
                return hwnd
            time.sleep(0.15)

        # Fallbacks for edge cases: minimized first, then hidden.
        hwnd = self._find_visible_window_for_pid(targets, include_minimized=True)
        if hwnd:
            return hwnd
        return self._find_visible_window_for_pid(targets, include_hidden=True, include_minimized=True)

    def _window_matches_geometry(self, hwnd: int, x: int, y: int, w: int, h: int, tol: int = 2) -> bool:
        try:
            l, t, r, b = win32gui.GetWindowRect(int(hwnd))
            cur_w = int(r - l)
            cur_h = int(b - t)
            return (
                abs(int(l) - int(x)) <= int(tol)
                and abs(int(t) - int(y)) <= int(tol)
                and abs(int(cur_w) - int(w)) <= int(tol)
                and abs(int(cur_h) - int(h)) <= int(tol)
            )
        except Exception:
            return False

    def _schedule_roblox_window_geometry_retry(self, user_id: str, pid: int) -> None:
        """Retry a failed launch-time geometry update once after Roblox settles."""
        try:
            timer = threading.Timer(
                5.0,
                self._maybe_enforce_roblox_window_geometry,
                args=(str(user_id), int(pid)),
                kwargs={"is_retry": True},
            )
            timer.daemon = True
            timer.start()
            self.log(f"[WINPOS RETRY] uid={user_id} pid={pid} scheduled_in=5s")
        except Exception:
            pass

    def _maybe_enforce_roblox_window_geometry(self, user_id: str, pid: int, *, is_retry: bool = False) -> None:
        try:
            if hasattr(self.cfg, "peek_settings"):
                settings = self.cfg.peek_settings() or {}
            elif hasattr(self.cfg, "load_settings"):
                settings = self.cfg.load_settings() or {}
            else:
                settings = {}
        except Exception:
            settings = {}

        rwg = settings.get("roblox_window_geometry", {}) or {}
        if not isinstance(rwg, dict):
            return
        if not bool(rwg.get("enforce_on_launch", False)):
            return

        try:
            x = int(rwg.get("x", 0) or 0)
            y = int(rwg.get("y", 0) or 0)
            w = int(rwg.get("w", 0) or 0)
            h = int(rwg.get("h", 0) or 0)
        except Exception:
            return

        if w <= 0 or h <= 0:
            return

        target_pids = self._related_window_search_pids(int(pid))
        hwnd = self._wait_for_visible_window_for_pid(target_pids, timeout_s=10.0)
        if not hwnd:
            try:
                self.log(f"[WINPOS SKIP] uid={user_id} pid={pid} reason=no_window")
            except Exception:
                pass
            return

        tol = 2
        if self._window_matches_geometry(hwnd, x, y, w, h, tol=tol):
            return

        try:
            import win32con
        except Exception as e:
            try:
                self.log(f"[WINPOS FAIL] uid={user_id} pid={pid} err={e!r}")
            except Exception:
                pass
            if not is_retry:
                self._schedule_roblox_window_geometry_retry(user_id, pid)
            return

        flags = int(win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        try:
            flags |= int(getattr(win32con, "SWP_ASYNCWINDOWPOS", 0))
        except Exception:
            pass

        last_err = None
        applied = False
        for attempt in range(1, 11):
            try:
                if not win32gui.IsWindow(int(hwnd)):
                    hwnd = self._find_visible_window_for_pid(target_pids, include_minimized=True)
                    if not hwnd:
                        target_pids = self._related_window_search_pids(target_pids)
                        hwnd = self._wait_for_visible_window_for_pid(target_pids, timeout_s=1.25)
                    if not hwnd:
                        break
            except Exception:
                hwnd = self._find_visible_window_for_pid(target_pids, include_minimized=True)
                if not hwnd:
                    break

            try:
                if win32gui.IsIconic(int(hwnd)) or win32gui.IsZoomed(int(hwnd)):
                    win32gui.ShowWindow(int(hwnd), int(win32con.SW_RESTORE))
                    time.sleep(0.06)
            except Exception:
                pass

            try:
                win32gui.SetWindowPos(int(hwnd), 0, int(x), int(y), int(w), int(h), flags)
            except Exception as e:
                last_err = e
                try:
                    # Fallback for some window-state/style transitions.
                    win32gui.MoveWindow(int(hwnd), int(x), int(y), int(w), int(h), True)
                except Exception as move_err:
                    last_err = move_err

            time.sleep(0.10 if attempt < 3 else 0.20)
            if self._window_matches_geometry(int(hwnd), x, y, w, h, tol=tol):
                applied = True
                break

            target_pids = self._related_window_search_pids(target_pids)
            hwnd = self._find_visible_window_for_pid(target_pids, include_minimized=True)

        if applied:
            try:
                self.log(f"[WINPOS] uid={user_id} pid={pid} -> x={x} y={y} w={w} h={h}")
            except Exception:
                pass
            return

        try:
            suffix = f" err={last_err!r}" if last_err is not None else ""
            self.log(
                f"[WINPOS FAIL] uid={user_id} pid={pid} "
                f"target=x={x} y={y} w={w} h={h}{suffix}"
            )
        except Exception:
            pass
        if not is_retry:
            self._schedule_roblox_window_geometry_retry(user_id, pid)


    def _convert_share_link(self, share_code, cookie):
        import requests, json
        cookie = str(cookie or "")
        if normalize_roblosecurity_cookie_value is not None:
            try:
                cookie = normalize_roblosecurity_cookie_value(cookie)
            except Exception:
                cookie = str(cookie or "")
        if not share_code or not cookie:
            return None, None, cookie
        url = "https://apis.roblox.com/sharelinks/v1/resolve-link"
        payload = {"linkId": share_code, "linkType": "Server"}
        s = requests.Session()
        try:
            s.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com", path="/")
        except Exception:
            s.cookies[".ROBLOSECURITY"] = cookie
        s.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.roblox.com/"
        })

        def _maybe_update_cookie(resp) -> None:
            nonlocal cookie
            if not resp or extract_roblosecurity_from_requests_response is None:
                return
            try:
                updated = extract_roblosecurity_from_requests_response(resp, session=s)
            except Exception:
                updated = None
            if updated and updated != cookie:
                cookie = updated
                try:
                    s.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com", path="/")
                except Exception:
                    pass
        try:
            r = s.post(url, json=payload, timeout=10)
            _maybe_update_cookie(r)
            if r.status_code == 403:
                csrf = r.headers.get("X-CSRF-TOKEN")
                if csrf:
                    s.headers["X-CSRF-TOKEN"] = csrf
                    r = s.post(url, json=payload, timeout=10)
                    _maybe_update_cookie(r)
            if r.status_code == 200:
                data = r.json()
                invite = data.get("privateServerInviteData") or {}
                place = str(invite.get("placeId") or "")
                link  = invite.get("linkCode")
                # NEW: remember this mapping globally so other users can compare labels
                try:
                    if link:
                        self.tracker.share_to_link[share_code] = {"place": place, "link": link}
                except Exception:
                    pass
                return place, link, cookie
        except Exception:
            pass
        return None, None, cookie

    def _get_share_resolver_cookies(self, *, prefer_cookie: str = "", exclude_uid: str = "") -> list[str]:
        """
        Return a prioritized list of cookies that can be used to resolve a Server share link.

        Share link resolution requires an authenticated request; alternate launch accounts may not
        have a cookie, so we can borrow any other stored cookie for the one-time resolve.
        """
        cookies: list[str] = []
        seen: set[str] = set()

        def _add(c: str) -> None:
            c = str(c or "").strip()
            if not c or c in seen:
                return
            seen.add(c)
            cookies.append(c)

        _add(prefer_cookie)

        try:
            peek = getattr(self.cfg, "peek_users", None)
            users = (peek() if callable(peek) else self.cfg.load_users()) or {}
        except Exception:
            users = {}

        if not isinstance(users, dict) or not users:
            return cookies

        # Prefer cookies from enabled, non-flagged, non-alternate accounts first.
        for uid, info in users.items():
            if exclude_uid and str(uid) == str(exclude_uid):
                continue
            if not isinstance(info, dict):
                continue
            if bool(info.get("alternate_launch", False)):
                continue
            if bool(info.get("disabled", False)) or bool(info.get("bad", False)) or bool(info.get("cap", False)):
                continue
            _add(info.get("cookie", ""))

        # Fallback: include any remaining cookies (even if flagged/disabled).
        for uid, info in users.items():
            if exclude_uid and str(uid) == str(exclude_uid):
                continue
            if not isinstance(info, dict):
                continue
            if bool(info.get("alternate_launch", False)):
                continue
            _add(info.get("cookie", ""))

        return cookies
    
    def compute_server_label(self, user_info: dict, cookie: str) -> str:
        """
        Return the exact server label we use at launch time:
        - Private server => first 10 chars of *linkCode* (not share code)
        - Public => 'Public:<placeId>'
        """
        psl = (user_info.get("private_server_link") or "").strip() if isinstance(user_info, dict) else ""
        place_cfg = user_info.get("place") if isinstance(user_info, dict) else None

        is_alternate = bool((user_info or {}).get("alternate_launch", False))

        # Parse quickly
        p, code, ltype, updated_cookie = self._extract_private_server_info(psl, cookie if not is_alternate else None)
        if (
            updated_cookie
            and cookie
            and updated_cookie != cookie
            and persist_updated_cookie is not None
        ):
            try:
                persist_updated_cookie(self.cfg, old_cookie=cookie, new_cookie=updated_cookie)
            except Exception:
                pass
            cookie = updated_cookie

        # If it's a SHARE link, prefer any previously learned mapping → linkCode
        if ltype == "share" and code:
            try:
                m = self.tracker.share_to_link.get(code)
            except Exception:
                m = None
            if m and m.get("link"):
                # adopt mapped values (now equivalent to a resolved direct link)
                p, code, ltype = (m.get("place") or ""), (m.get("link") or ""), "resolved"
            else:
                # try to resolve with the current cookie (may fail — that's fine)
                rp, rc, new_cookie = self._convert_share_link(code, cookie)
                if (
                    new_cookie
                    and cookie
                    and new_cookie != cookie
                    and persist_updated_cookie is not None
                ):
                    try:
                        persist_updated_cookie(self.cfg, old_cookie=cookie, new_cookie=new_cookie)
                    except Exception:
                        pass
                    cookie = new_cookie
                if rp and rc:
                    p, code, ltype = rp, rc, "resolved"
                elif is_alternate:
                    for resolver_cookie in self._get_share_resolver_cookies(prefer_cookie=cookie):
                        rp, rc, new_resolver_cookie = self._convert_share_link(code, resolver_cookie)
                        if (
                            new_resolver_cookie
                            and resolver_cookie
                            and new_resolver_cookie != resolver_cookie
                            and persist_updated_cookie is not None
                        ):
                            try:
                                persist_updated_cookie(self.cfg, old_cookie=resolver_cookie, new_cookie=new_resolver_cookie)
                            except Exception:
                                pass
                        if rp and rc:
                            p, code, ltype = rp, rc, "resolved"
                            break

        target_place = str(p or place_cfg or self.target_place)
        return (code[:10] if code else f"Public:{target_place}")


    def start_game_session(self, user_id, cookie, user_info=None, skip_cleanup=False):
        import os, time, random

        launch_ts = time.time()
        uid_key = str(user_id)
        original_cookie = str(cookie or "")

        # Fast-path: if this uid is already mid-launch, skip BEFORE any network/auth work or logs.
        with GameLauncher._global_launch_inflight_lock:
            if uid_key in GameLauncher._global_launch_inflight:
                server_label = "Unknown"
                try:
                    us = getattr(self.tracker, "user_server", {}) or {}
                    server_label = str(us.get(user_id) or us.get(uid_key) or server_label)
                except Exception:
                    pass
                try:
                    self.log_skip(uid_key, server_label, "launch_inflight_global", throttle=2.0)
                except Exception:
                    try:
                        self.log(f"[LAUNCH SKIP] {uid_key} -> {server_label} launch_inflight_global")
                    except Exception:
                        pass
                return False

        with self._launch_inflight_lock:
            if uid_key in self._launch_inflight:
                server_label = "Unknown"
                try:
                    us = getattr(self.tracker, "user_server", {}) or {}
                    server_label = str(us.get(user_id) or us.get(uid_key) or server_label)
                except Exception:
                    pass
                try:
                    self.log_skip(uid_key, server_label, "launch_inflight", throttle=2.0)
                except Exception:
                    try:
                        self.log(f"[LAUNCH SKIP] {uid_key} -> {server_label} launch_inflight")
                    except Exception:
                        pass
                return False

        # pull original config
        psl = ""
        if user_info and isinstance(user_info, dict):
            psl = user_info.get("private_server_link", "")

        is_alternate = bool((user_info or {}).get("alternate_launch", False))

        # parse target place / link-code (resolve share links early)
        place_id, private_code, link_type, updated_cookie = self._extract_private_server_info(
            psl, cookie if not is_alternate else None
        )
        if updated_cookie and cookie and updated_cookie != cookie:
            cookie = updated_cookie
            if persist_updated_cookie is not None and original_cookie and cookie != original_cookie:
                try:
                    persist_updated_cookie(self.cfg, user_id=str(user_id), new_cookie=cookie)
                    original_cookie = cookie
                except Exception:
                    pass
        if link_type == "share" and private_code:
            try:
                m = self.tracker.share_to_link.get(private_code)
            except Exception:
                m = None
            if m and m.get("place") and m.get("link"):
                place_id, private_code, link_type = str(m.get("place") or ""), str(m.get("link") or ""), "resolved"
            else:
                if is_alternate:
                    for resolver_cookie in self._get_share_resolver_cookies(prefer_cookie=cookie, exclude_uid=str(user_id)):
                        rp, rc, new_resolver_cookie = self._convert_share_link(private_code, resolver_cookie)
                        if (
                            new_resolver_cookie
                            and resolver_cookie
                            and new_resolver_cookie != resolver_cookie
                            and persist_updated_cookie is not None
                        ):
                            try:
                                persist_updated_cookie(self.cfg, old_cookie=resolver_cookie, new_cookie=new_resolver_cookie)
                            except Exception:
                                pass
                        if rp and rc:
                            place_id, private_code, link_type = rp, rc, "resolved"
                            break
                else:
                    rp, rc, new_cookie = self._convert_share_link(private_code, cookie)
                    if new_cookie and cookie and new_cookie != cookie:
                        cookie = new_cookie
                        if persist_updated_cookie is not None and original_cookie and cookie != original_cookie:
                            try:
                                persist_updated_cookie(self.cfg, user_id=str(user_id), new_cookie=cookie)
                                original_cookie = cookie
                            except Exception:
                                pass
                    if rp and rc:
                        place_id, private_code, link_type = rp, rc, "resolved"

        user_place_cfg = user_info.get("place") if isinstance(user_info, dict) else None
        target_place = place_id or user_place_cfg or self.target_place

        # server label (keep short-code for PS; public = place)
        server_label = (f"{(private_code or '')[:10]}" if private_code else f"Public:{target_place}")
        is_public = server_label.startswith("Public:")

        # High-signal launch attempt log (works in GUI + console)
        self.log(
            f"[LAUNCH] uid={user_id} place={target_place} label={server_label} "
            f"private={'yes' if private_code else 'no'} link_type={link_type} "
            f"mode={'alternate' if is_alternate else 'cookie'} "
            f"skip_cleanup={'yes' if skip_cleanup else 'no'}"
        )

        # ---- Reservation guard (prevents races vs handoff pre-joins) --------------
        allow_shared = bool((user_info or {}).get("allow_shared_server"))
        r = None  # IMPORTANT: always initialize so allow_shared=True doesn't break
        if not allow_shared:
            rs = getattr(self.tracker, "reserved_servers", {})
            r = rs.get(server_label)
        if r is not None and r.get("by") != user_id and r.get("exp", 0) > time.time():
            # throttle logging & backoff
            now = time.time()
            key = (user_id, server_label)
            if self._skip_log_until.get(key, 0) <= now:
                self._skip_log_until[key] = now + 8
                self.log(f"[LAUNCH SKIP] {user_id} -> {server_label} reserved by {r.get('by')} ({r.get('type')})")
            try:
                self.tracker.skip_until_by_user[user_id] = now + 10
            except Exception:
                pass
            return False

        # ---- One-per-server guard (live occupants) --------------------------------
        if not allow_shared and not is_public:
            for other_uid, other_label in (self.tracker.user_server or {}).items():
                if other_uid != user_id and other_label == server_label:
                    now = time.time()
                    key = (user_id, server_label)
                    if self._skip_log_until.get(key, 0) <= now:
                        self._skip_log_until[key] = now + 8
                        self.log(f"[LAUNCH SKIP] {user_id} -> {server_label} already occupied by {other_uid}")
                    try:
                        self.tracker.skip_until_by_user[user_id] = now + 30
                    except Exception:
                        pass
                    return False
        # ---- Build URL --------------------------------------------------------------
        if is_alternate:
            # Alternate mode: no cookies/auth ticket; launch via roblox:// protocol.
            # Public:  roblox://placeId=<place>
            # Private: roblox://placeId=<place>&linkCode=<code>
            if link_type == "share":
                self.log(f"[LAUNCH FAIL] uid={user_id} label={server_label} reason=share_link_unresolved")
                return False
            game_url = f"roblox://placeId={target_place}"
            if private_code:
                game_url += f"&linkCode={private_code}"
        else:
            auth_ticket, new_cookie, auth_meta = self.auth_handler.obtain_auth_ticket(cookie)
            if new_cookie and cookie and new_cookie != cookie:
                cookie = new_cookie
                if persist_updated_cookie is not None and original_cookie and cookie != original_cookie:
                    try:
                        persist_updated_cookie(self.cfg, user_id=str(user_id), new_cookie=cookie)
                        original_cookie = cookie
                    except Exception:
                        pass
            if not auth_ticket:
                auth_meta = auth_meta or {}
                failure_reason = str(auth_meta.get("failure_reason") or "no_response")
                should_mark_bad = bool(auth_meta.get("mark_bad", False))
                if should_mark_bad:
                    try:
                        should_mark_bad = bool(self.cfg.auto_bad_marking_enabled())
                    except Exception:
                        should_mark_bad = True
                self.log(
                    f"[LAUNCH FAIL] uid={user_id} label={server_label} "
                    f"reason=no_auth_ticket auth={failure_reason}"
                )
                if should_mark_bad:
                    self.cfg.mark_bad_cookie(user_id, True)
                if user_info is not None and should_mark_bad:
                    user_info["bad"] = True
                    user_info["inactive_since"] = time.time()
                return False

            browser_id = f"{random.randint(100000,130000)}{random.randint(100000,900000)}"
            if private_code:
                launcher_url = (
                    "https://assetgame.roblox.com/game/PlaceLauncher.ashx"
                    f"?request=RequestPrivateGame&placeId={target_place}&linkCode={private_code}"
                )
            else:
                launcher_url = (
                    "https://assetgame.roblox.com/game/PlaceLauncher.ashx"
                    f"?request=RequestGame&placeId={target_place}"
                )

            game_url = (
                "roblox-player://1/1+launchmode:play"
                f"+gameinfo:{auth_ticket}"
                f"+launchtime:{int(launch_ts * 1000)}"
                f"+browsertrackerid:{browser_id}"
                f"+placelauncherurl:{launcher_url}"
                "+robloxLocale:en_us+gameLocale:en_us"
            )

        # Per-user "inflight" launch guard: avoids double os.startfile()/PID waits for the same uid.
        with GameLauncher._global_launch_inflight_lock:
            if uid_key in GameLauncher._global_launch_inflight:
                try:
                    self.log_skip(uid_key, server_label, "launch_inflight_global", throttle=2.0)
                except Exception:
                    try:
                        self.log(f"[LAUNCH SKIP] {uid_key} -> {server_label} launch_inflight_global")
                    except Exception:
                        pass
                return False
            GameLauncher._global_launch_inflight.add(uid_key)

        with self._launch_inflight_lock:
            if uid_key in self._launch_inflight:
                with GameLauncher._global_launch_inflight_lock:
                    GameLauncher._global_launch_inflight.discard(uid_key)
                try:
                    self.log_skip(uid_key, server_label, "launch_inflight", throttle=2.0)
                except Exception:
                    try:
                        self.log(f"[LAUNCH SKIP] {uid_key} -> {server_label} launch_inflight")
                    except Exception:
                        pass
                return False
            self._launch_inflight.add(uid_key)

        merchant_prelaunch_applied = False
        merchant_controller = get_merchant_fix_controller()
        try:
            with GameLauncher._global_launch_attempt_lock:
                if not skip_cleanup:
                    for pid in self.tracker.user_processes.get(user_id, []).copy():
                        if pid != self.process_manager.excluded_pid:
                            self.process_manager.terminate_process(pid, self.tracker)

                merchant_prelaunch_applied = bool(
                    merchant_controller.before_roblox_launch(user_id)
                )
                pid_launch_ts = time.time()
                self.log(f"[LAUNCH] uid={user_id} label={server_label} calling os.startfile()")
                os.startfile(game_url)
                self.log(f"[LAUNCH] uid={user_id} label={server_label} startfile returned, waiting for PID...")

                # Allow ProcessManager.await_new_process to emit into the same log sink
                if getattr(self, "tracker", None) is not None:
                    try:
                        self.tracker.debug_log = self.log
                    except Exception:
                        pass

                new_pid = self.process_manager.await_new_process(user_id, pid_launch_ts, self.process_timeout, self.tracker)
                if new_pid and merchant_prelaunch_applied:
                    try:
                        process_created_at = float(psutil.Process(int(new_pid)).create_time())
                        merchant_controller.on_roblox_process_created(
                            int(new_pid), user_id, process_created_at
                        )
                        merchant_prelaunch_applied = False
                    except Exception:
                        try:
                            self.process_manager.terminate_process(int(new_pid), self.tracker)
                        except Exception:
                            pass
                        raise
            if new_pid:
                # clear bad flag if we just launched fine
                if user_info and user_info.get("bad", False):
                    self.cfg.mark_bad_cookie(user_id, False)
                    user_info["bad"] = False

                # record the live server label/code/place
                self.tracker.user_server[user_id]    = server_label
                self.tracker.user_ps_place[user_id]  = str(target_place)
                self.tracker.user_ps_code[user_id]   = private_code or ""

                # cache resolved owner
                owner_username = self._find_ps_owner_username(psl, private_code)
                if not owner_username and isinstance(user_info, dict):
                    owner_username = (user_info.get("username") or "").strip()
                self.tracker.server_owner[user_id] = owner_username or ""

                # release any reservation held by this uid (if present)
                try:
                    rs = getattr(self.tracker, "reserved_servers", {})
                    for lbl, meta in list(rs.items()):
                        if meta.get("by") == user_id:
                            rs.pop(lbl, None)
                except Exception:
                    pass

                try:
                    self._maybe_enforce_roblox_window_geometry(str(user_id), int(new_pid))
                except Exception:
                    pass

                try:
                    self._record_account_launch_activity(user_id, time.time())
                except Exception:
                    pass

                return True

            # failed to see a process — leave cleanup to caller/TTL
            self.log(
                f"[LAUNCH FAIL] uid={user_id} label={server_label} reason=pid_not_found "
                f"timeout={self.process_timeout}"
            )
            return False

        except Exception as e:
            # on exception, do nothing; TTL will prune any stale reservations
            try:
                self.log(f"[LAUNCH ERROR] uid={user_id} label={server_label} err={e!r}")
                import traceback
                self.log(f"[LAUNCH TRACE] uid={user_id} label={server_label}\n{traceback.format_exc()}")
            except Exception:
                pass
            return False
        finally:
            if merchant_prelaunch_applied:
                merchant_controller.cancel_prelaunch(user_id, "launch did not produce a usable Roblox process")
            with self._launch_inflight_lock:
                self._launch_inflight.discard(uid_key)
            with GameLauncher._global_launch_inflight_lock:
                GameLauncher._global_launch_inflight.discard(uid_key)


    def initialize_all_sessions(self, user_configs: dict):
        import time
        self.tracker.initialization_mode = True
        try:
            ordered_configs = sort_user_items_by_launch_priority((user_configs or {}).items())
            for idx, (user_id, user_info) in enumerate(ordered_configs):
                if user_info.get("bad", False) or user_info.get("cap", False):
                    continue
                cookie = user_info.get("cookie", "") if isinstance(user_info, dict) else user_info
                for pid in self.tracker.user_processes.get(user_id, []).copy():
                    if self.process_manager.verify_process_active(pid):
                        self.process_manager.terminate_process(pid, self.tracker)
                self.start_game_session(user_id, cookie, user_info, skip_cleanup=True)
                if idx < len(ordered_configs) - 1:
                    time.sleep(self.initial_delay)
        finally:
            self.tracker.initialization_mode = False
        # --- PS owner resolution helpers ----------------------------------------

    def _extract_code_quick(self, link: str) -> str:
        """Best-effort parse of a private server code from a link (no network)."""
        if not link:
            return ""
        import re
        # Direct link: ...?privateServerLinkCode=XXXXXXXX
        m = re.search(r'privateServerLinkCode=([A-Za-z0-9_-]+)', link)
        if m:
            return m.group(1)
        # Share link: .../share?code=XXXXXXXX&type=Server
        m = re.search(r'/share\?code=([A-Za-z0-9_-]+)&type=Server', link)
        if m:
            return m.group(1)
        return ""

    def _find_ps_owner_username(self, psl: str, private_code: str = "") -> str:
        """
        Determine the PS owner by comparing the current link/code to the users.json entries.
        Rule: the user whose configured private_server_link matches (by exact link OR by code)
              is considered the owner.
        """
        try:
            peek = getattr(self.cfg, "peek_users", None)
            users = (peek() if callable(peek) else self.cfg.load_users()) or {}
        except Exception:
            users = {}

        # Normalize target
        target_code = (private_code or self._extract_code_quick(psl) or "").strip()
        target_link = (psl or "").strip()

        # 1) Exact link match
        if target_link:
            for _, info in users.items():
                if isinstance(info, dict) and (info.get("private_server_link") or "").strip() == target_link:
                    return (info.get("username") or "").strip()

        # 2) Code match
        if target_code:
            for _, info in users.items():
                link = (info.get("private_server_link") or "").strip()
                if not link:
                    continue
                code = self._extract_code_quick(link)
                if code and code == target_code:
                    return (info.get("username") or "").strip()

        return ""



# ──────────────────────────────────────────────────────────────
# 1-C. execute_main_loop – new “process-only” heartbeat
# ──────────────────────────────────────────────────────────────
def execute_main_loop():
    manager      = RobloxManager()
    process_mgr  = ProcessManager(manager.excluded_pid)
    launcher = GameLauncher(
        manager.target_place,
        process_mgr,
        manager.auth_handler,
        manager.process_tracker,
        manager.config_manager,
        launch_delay=manager.timeouts["launch_delay"],
        initial_delay=manager.timeouts["initial_delay"]
)


    # track the last launch so we honour launch_delay
    user_state = {
        uid: {"last_launch": 0,
              "log_miss_streak": 0,
              "log_generation_baseline": "",
              "user_info" : info}
        for uid, info in sort_user_items_by_launch_priority(manager.settings.items())
    }

    for uid, st in user_state.items():
        uname = str((st.get("user_info") or {}).get("username", "") or "").strip().lower()
        if uname:
            baseline = find_log_match(uname).match
            st["log_generation_baseline"] = baseline.generation_id if baseline else ""

    # fire everything once on boot
    launcher.initialize_all_sessions(manager.settings)
    for uid in user_state:
        user_state[uid]["last_launch"] = time.time()
    last_global_launch_at = time.time()

    # ───── main loop ─────
    tickers = {'window': 0, 'cleanup': 0}
    cap_watchdog_cfg = normalize_cap_watchdog_settings(
        (manager.config_manager.load_settings() or {}).get("cap_watchdog")
    )
    preconnect_tracker = PreconnectTracker(
        grace_seconds=cap_watchdog_cfg["missing_username_timeout_seconds"]
    )
    while True:
        now = time.time()

        # housekeeping
        if now - tickers['cleanup'] >= manager.check_intervals['cleanup']:
            process_mgr.cleanup_dead_processes(manager.process_tracker)
            process_mgr.eliminate_orphaned_processes(
                manager.process_tracker, set(manager.settings.keys())
            )
            tickers['cleanup'] = now

        if now - tickers['window'] >= manager.check_intervals['window']:
            for pid, nwin in process_mgr.count_windows_by_process().items():
                if nwin > manager.window_limit and pid != manager.excluded_pid:
                    process_mgr.terminate_process(pid, manager.process_tracker)
            tickers['window'] = now
            
        # --- NEW: pre-connect watchdog (headless) -----------------------------
        now = time.time()

        for uid, pids in list(manager.process_tracker.user_processes.items()):
            uid_s = str(uid)
            live_pids = [pid for pid in pids if process_mgr.verify_process_active(pid)]
            if not live_pids:
                preconnect_tracker.reset(uid_s)
                continue

            info = manager.settings.get(uid_s, {}) or {}
            uname = str(info.get("username", "")).lower()
            if not uname:
                preconnect_tracker.reset(uid_s)
                continue  # nothing to check

            process_ct = max(manager.process_tracker.creation_timestamps.get(pid, now) for pid in live_pids)
            lookup = find_log_match(uname, not_before=float(process_ct) - 15.0)
            baseline_generation = str(
                (user_state.get(uid_s, {}) or {}).get("log_generation_baseline", "") or ""
            )
            if lookup.match and baseline_generation == lookup.match.generation_id:
                lookup = LogLookupResult("conclusively_missing", health=lookup.health)
            decision = preconnect_tracker.observe(
                uid_s,
                launch_token=float(process_ct),
                live=True,
                lookup=lookup,
            )
            if decision == "confirmed":
                try:
                    user_state[uid_s]["log_miss_streak"] = 0
                    user_state[uid_s]["log_generation_baseline"] = ""
                except Exception:
                    pass
            elif decision == "timed_out":
                    st0 = user_state.get(uid_s, {})
                    mark_launch_last_once(st0)
                    user_state[uid_s] = st0
                    # failed to ever attach to a log with the username — recycle it
                    if not bool(info.get("cap", False)):
                        try:
                            streak, _counted, reached_limit = increment_cap_counter(
                                st0,
                                enabled=cap_watchdog_cfg["missing_username_increments_cap"],
                                limit=cap_watchdog_cfg["cap_counter_limit"],
                            )
                            user_state[uid_s] = st0
                        except Exception:
                            streak = 0
                            reached_limit = False

                        if reached_limit:
                            try:
                                manager.config_manager.mark_cap_flag(uid_s, True)
                            except Exception:
                                pass
                            try:
                                info["cap"] = True
                                manager.settings[uid_s]["cap"] = True
                                user_state[uid_s]["user_info"]["cap"] = True
                            except Exception:
                                pass
                    for pid in live_pids:
                        process_mgr.terminate_process(pid, manager.process_tracker)
                    manager.process_tracker.user_server[uid_s] = "DISCONNECTED"
                    preconnect_tracker.reset(uid_s)
        # --- END new watchdog --------------------------------------------------

        # --- build eligible candidates for this tick ---------------------------------
        eligible = []   # list of tuples (uid, st, cookie, info, server_label)

        # snapshot once for speed/readability
        servers_live = dict(manager.process_tracker.user_server or {})
        skip_ttl     = dict(manager.process_tracker.skip_until_by_user or {})
        reserved     = dict(manager.process_tracker.reserved_servers or {})

        for uid, st in user_state.items():
            # 1) live process? then skip (but keep 'Disconnected' marker fresh)
            live_pids = [pid for pid in manager.process_tracker.user_processes.get(uid, [])
                        if process_mgr.verify_process_active(pid)]
            if not live_pids:
                grace_until = 0
                try:
                    grace_until = (manager.process_tracker.pid_grace_until or {}).get(uid, 0)
                except Exception:
                    grace_until = 0
                if not (grace_until and now < grace_until):
                    manager.process_tracker.user_server[uid] = "DISCONNECTED"
            if live_pids:
                continue

            # 2) honor this account's launch_delay
            if (now - st["last_launch"]) < manager.timeouts['launch_delay']:
                continue

            # 3) per-user backoff after a skip
            if now < skip_ttl.get(uid, 0):
                continue

            # 4) compute target label exactly as launcher will
            info   = st["user_info"] if isinstance(st["user_info"], dict) else {}
            if isinstance(info, dict) and (info.get("bad", False) or info.get("cap", False) or info.get("disabled", False)):
                continue
            cookie = info.get("cookie", "") if isinstance(info, dict) else info
            server_label = launcher.compute_server_label(info, cookie)
            is_public = server_label.startswith("Public:")

            # 5) preflight checks with LOGGING (do NOT bump last_launch on skips)
            #    (a) already occupied by someone else?
            if not is_public:
                occupied_by = next(
                    (other_uid for other_uid, other_label in servers_live.items()
                    if other_uid != uid and other_label == server_label),
                    None
                )
                if occupied_by:
                    manager.process_tracker.skip_until_by_user[uid] = now + 30
                    launcher.log_skip(uid, server_label, f"already occupied by {occupied_by}")
                    continue

            #    (b) reserved by an in-flight handoff/normal?
            r = reserved.get(server_label)
            if r and r.get("by") != uid and r.get("exp", 0) > now:
                manager.process_tracker.skip_until_by_user[uid] = now + 10
                launcher.log_skip(uid, server_label, f"reserved by {r.get('by')} ({r.get('type')})")
                continue

            #    (c) same-owner guard (avoid launching into a PS whose owner is already active)
            pslink = (info.get("private_server_link") or "").strip()
            owner  = launcher._find_ps_owner_username(
                pslink, "" if server_label.startswith("Public:") else server_label
            )
            if owner:
                owner_lc = owner.strip().lower()
                same_owner_live = any(
                    (other_uid != uid) and
                    ((manager.settings.get(other_uid, {}).get("username") or "").strip().lower() == owner_lc)
                    for other_uid in servers_live
                )
                if same_owner_live:
                    manager.process_tracker.skip_until_by_user[uid] = now + 30
                    launcher.log_skip(uid, server_label, "same owner already active")
                    continue

            # if we got here, this uid is a valid candidate for launching this tick
            eligible.append((uid, st, cookie, info, server_label))

        # --- deterministic: one-shot demotions last, then launch priority ---
        eligible = [
            row
            for _idx, row in sorted(
                enumerate(eligible),
                key=lambda pair: launch_queue_sort_key(
                    pair[1][0],
                    pair[1][3] if isinstance(pair[1][3], dict) else {},
                    pair[1][1] if isinstance(pair[1][1], dict) else {},
                    pair[0],
                ),
            )
        ]

        global_launch_delay = max(
            0.0,
            float(manager.timeouts.get('launch_delay', 0) or 0),
        )
        if (now - last_global_launch_at) < global_launch_delay:
            eligible = []

        for uid, st, cookie, info, server_label in eligible:
            uname = str(info.get("username", "") or "").strip().lower() if isinstance(info, dict) else ""
            baseline = find_log_match(uname).match if uname else None
            st["log_generation_baseline"] = baseline.generation_id if baseline else ""
            attempt_started = time.time()
            last_global_launch_at = attempt_started
            consume_launch_last_once(st)
            ok = launcher.start_game_session(uid, cookie, info)
            st["last_launch"] = attempt_started  # every attempt consumes the slot
            preconnect_tracker.reset(uid)
            if not ok:
                break
            if ok:
                break                         # launched one → stop this tick

        time.sleep(manager.check_intervals['main_tick'])

if __name__ == "__main__":
    # Needed for frozen executables (Nuitka/PyInstaller) that use multiprocessing/ProcessPoolExecutor.
    from multiprocessing import freeze_support

    freeze_support()
    execute_main_loop()
