"""
Auto Actions automation engine for JARAM.

This owns the low-level click/paste/window helpers used by the Auto-Actions
rule engine.
"""

from __future__ import annotations

import atexit
import copy
import ctypes
import datetime as _dt
import difflib
import io
import json
import re
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import win32api
import win32con
import win32gui
import win32process

try:
    import autoit  # type: ignore
except Exception:  # pragma: no cover
    autoit = None  # type: ignore

try:
    from PIL import ImageGrab
except Exception:  # pragma: no cover
    ImageGrab = None  # type: ignore


@dataclass(frozen=True)
class RelPoint:
    """Point stored as percentage of the Roblox window client area."""

    x: float
    y: float


def _normalize_user_id_list(raw: object) -> Optional[List[str]]:
    """
    Normalize a per-rule users filter to a list of string UIDs.

    Returns:
      - None: no filter
      - []: explicit empty selection
      - [uids...]: explicit filter list
    """
    if raw is None:
        return None

    seq: Iterable
    if isinstance(raw, (list, tuple, set)):
        seq = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except Exception:
            parts = [p.strip() for p in s.replace("\r", "\n").replace("\n", ",").split(",")]
            seq = [p for p in parts if p]
        else:
            if isinstance(parsed, (list, tuple, set)):
                seq = parsed
            else:
                seq = [parsed]
    else:
        try:
            if isinstance(raw, dict):
                return None
            seq = list(raw)  # type: ignore[arg-type]
        except Exception:
            return None

    return [str(u).strip() for u in seq if str(u).strip()]


def _clamp01(v: float) -> float:
    try:
        if v < 0.0:
            return 0.0
        if v > 1.0:
            return 1.0
        return float(v)
    except Exception:
        return 0.0


def _hex_to_rgb(color_hex: str) -> Tuple[int, int, int]:
    s = (color_hex or "").strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) != 6:
        return (0, 0, 0)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return (0, 0, 0)


def _color_close(a: Tuple[int, int, int], b: Tuple[int, int, int], tol: int) -> bool:
    tol = int(tol or 0)
    return all(abs(int(x) - int(y)) <= tol for x, y in zip(a, b))


def _client_origin_and_size(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    try:
        left, top = win32gui.ClientToScreen(hwnd, (0, 0))
        _l, _t, right, bottom = win32gui.GetClientRect(hwnd)
        width = int(right - _l)
        height = int(bottom - _t)
        if width <= 0 or height <= 0:
            return None
        return int(left), int(top), width, height
    except Exception:
        return None


def _abs_from_rel(hwnd: int, p: RelPoint) -> Optional[Tuple[int, int]]:
    base = _client_origin_and_size(hwnd)
    if not base:
        return None
    left, top, width, height = base
    x = left + int(_clamp01(p.x) * width)
    y = top + int(_clamp01(p.y) * height)
    return x, y


_BLOCK_USER_MOUSE_MOVE_ENABLED: bool = False
_ALLOWED_MOVE_X: int = 0
_ALLOWED_MOVE_Y: int = 0
_ALLOWED_MOVE_UNTIL: float = 0.0
_ULONG_PTR = getattr(wintypes, "ULONG_PTR", ctypes.c_size_t)
_LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
_WPARAM = getattr(wintypes, "WPARAM", ctypes.c_size_t)
_LPARAM = getattr(wintypes, "LPARAM", ctypes.c_ssize_t)


def _note_program_mouse_target(x: int, y: int, *, hold_s: float = 0.25) -> None:
    """
    Tell the low-level mouse hook which cursor positions are expected from automation.
    """
    global _ALLOWED_MOVE_X, _ALLOWED_MOVE_Y, _ALLOWED_MOVE_UNTIL
    try:
        _ALLOWED_MOVE_X = int(x)
        _ALLOWED_MOVE_Y = int(y)
        _ALLOWED_MOVE_UNTIL = float(time.monotonic()) + float(max(0.0, hold_s))
    except Exception:
        pass


def _mouse_move_instant(x: int, y: int) -> None:
    x = int(x)
    y = int(y)

    if autoit is None:
        return

    try:
        _note_program_mouse_target(x, y)
        autoit.mouse_move(x, y, speed=0)
    except Exception:
        pass


class _LL_POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", _LL_POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _UserMouseMoveBlocker:
    """
    Low-level mouse hook that blocks physical mouse movement while actions run.
    """

    WH_MOUSE_LL = 14
    WM_MOUSEMOVE = 0x0200
    WM_NCMOUSEMOVE = 0x00A0
    WM_QUIT = 0x0012
    LLMHF_INJECTED = 0x00000001
    LLMHF_LOWER_IL_INJECTED = 0x00000002

    LowLevelMouseProc = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, _WPARAM, _LPARAM)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._refcount = 0
        self._warned = False
        self._failed = False
        self._last_error: int = 0

        self._thread: Optional[threading.Thread] = None
        self._thread_id: int = 0
        self._hook = None
        self._ready = threading.Event()

        self._user32 = None
        self._kernel32 = None

        self._proc = self.LowLevelMouseProc(self._hook_proc)

    def acquire(self, *, log_fn: Optional[Callable[[str], None]] = None) -> bool:
        global _BLOCK_USER_MOUSE_MOVE_ENABLED
        with self._lock:
            if self._failed:
                if log_fn and not self._warned:
                    self._warned = True
                    msg = "[Auto-Actions] Mouse-move block is unavailable on this system/build."
                    if int(self._last_error or 0) != 0:
                        msg += f" (winerr={int(self._last_error)})"
                    log_fn(msg)
                return False

            ok = self._ensure_hook_installed()
            if not ok:
                self._failed = True
                if log_fn and not self._warned:
                    self._warned = True
                    msg = "[Auto-Actions] Failed to enable mouse-move block (hook install failed)."
                    if int(self._last_error or 0) != 0:
                        msg += f" (winerr={int(self._last_error)})"
                    log_fn(msg)
                return False

            self._refcount += 1
            _BLOCK_USER_MOUSE_MOVE_ENABLED = True
            return True

    def release(self) -> None:
        global _BLOCK_USER_MOUSE_MOVE_ENABLED
        should_shutdown = False
        with self._lock:
            if self._refcount <= 0:
                _BLOCK_USER_MOUSE_MOVE_ENABLED = False
                return
            self._refcount -= 1
            if self._refcount <= 0:
                _BLOCK_USER_MOUSE_MOVE_ENABLED = False
                should_shutdown = True
        if should_shutdown:
            self.shutdown(timeout_s=1.0)

    def shutdown(self, timeout_s: float = 2.0) -> None:
        global _BLOCK_USER_MOUSE_MOVE_ENABLED
        thread: Optional[threading.Thread] = None
        with self._lock:
            self._refcount = 0
            _BLOCK_USER_MOUSE_MOVE_ENABLED = False
            thread = self._thread
            thread_id = int(self._thread_id or 0)
            user32 = self._user32
            if user32 is not None and thread_id:
                try:
                    user32.PostThreadMessageW.argtypes = [
                        wintypes.DWORD,
                        wintypes.UINT,
                        _WPARAM,
                        _LPARAM,
                    ]
                    user32.PostThreadMessageW.restype = wintypes.BOOL
                except Exception:
                    pass
                try:
                    user32.PostThreadMessageW(thread_id, self.WM_QUIT, 0, 0)
                except Exception:
                    pass

        if thread and thread.is_alive() and thread is not threading.current_thread():
            try:
                thread.join(timeout=max(0.0, float(timeout_s)))
            except Exception:
                pass

        with self._lock:
            if thread and thread.is_alive() and self._hook and self._user32 is not None:
                try:
                    self._user32.UnhookWindowsHookEx(self._hook)
                except Exception:
                    pass
                self._hook = None
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def _ensure_hook_installed(self) -> bool:
        if self._hook:
            return True

        if self._thread and self._thread.is_alive():
            self._ready.wait(timeout=1.0)
            return bool(self._hook)

        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main, name="AutoActionMouseMoveBlocker", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=1.0)
        return bool(self._hook)

    def _thread_main(self) -> None:
        hook = None
        try:
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            try:
                self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD
                self._kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
                self._kernel32.GetModuleHandleW.restype = wintypes.HMODULE

                self._user32.SetWindowsHookExW.argtypes = [
                    ctypes.c_int,
                    self.LowLevelMouseProc,
                    wintypes.HINSTANCE,
                    wintypes.DWORD,
                ]
                self._user32.SetWindowsHookExW.restype = ctypes.c_void_p
                self._user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
                self._user32.UnhookWindowsHookEx.restype = wintypes.BOOL
                self._user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, _WPARAM, _LPARAM]
                self._user32.CallNextHookEx.restype = _LRESULT

                self._user32.PeekMessageW.argtypes = [
                    ctypes.POINTER(wintypes.MSG),
                    wintypes.HWND,
                    wintypes.UINT,
                    wintypes.UINT,
                    wintypes.UINT,
                ]
                self._user32.PeekMessageW.restype = wintypes.BOOL
                self._user32.GetMessageW.argtypes = [
                    ctypes.POINTER(wintypes.MSG),
                    wintypes.HWND,
                    wintypes.UINT,
                    wintypes.UINT,
                ]
                self._user32.GetMessageW.restype = ctypes.c_int
                self._user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
                self._user32.TranslateMessage.restype = wintypes.BOOL
                self._user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
                self._user32.DispatchMessageW.restype = _LRESULT
            except Exception:
                pass

            try:
                self._thread_id = int(self._kernel32.GetCurrentThreadId())
            except Exception:
                self._thread_id = 0

            try:
                msg = wintypes.MSG()
                self._user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 0)
            except Exception:
                pass

            try:
                hmod = self._kernel32.GetModuleHandleW(None)
            except Exception:
                hmod = 0

            try:
                ctypes.set_last_error(0)
            except Exception:
                pass
            hook = self._user32.SetWindowsHookExW(int(self.WH_MOUSE_LL), self._proc, hmod, 0)
            self._hook = hook
            if not hook:
                try:
                    self._last_error = int(ctypes.get_last_error() or 0)
                except Exception:
                    self._last_error = 0
            else:
                self._last_error = 0
        except Exception:
            hook = None
            self._hook = None
            self._thread_id = 0
        finally:
            self._ready.set()

        if not hook:
            return

        try:
            msg = wintypes.MSG()
            while True:
                res = self._user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
                if res == 0 or res == -1:
                    break
                try:
                    self._user32.TranslateMessage(ctypes.byref(msg))
                    self._user32.DispatchMessageW(ctypes.byref(msg))
                except Exception:
                    pass
        finally:
            try:
                if self._hook:
                    self._user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            self._hook = None

    def _hook_proc(self, nCode: int, wParam: int, lParam: int):
        user32 = self._user32
        if user32 is None:
            try:
                user32 = ctypes.windll.user32
            except Exception:
                user32 = None

        if nCode < 0 or not _BLOCK_USER_MOUSE_MOVE_ENABLED:
            if user32 is not None:
                try:
                    return user32.CallNextHookEx(0, nCode, wParam, lParam)
                except Exception:
                    return 0
            return 0

        msg = int(wParam)
        if msg not in (self.WM_MOUSEMOVE, self.WM_NCMOUSEMOVE):
            if user32 is not None:
                try:
                    return user32.CallNextHookEx(0, nCode, wParam, lParam)
                except Exception:
                    return 0
            return 0

        try:
            info = ctypes.cast(lParam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            flags = int(info.flags)
            if flags & (self.LLMHF_INJECTED | self.LLMHF_LOWER_IL_INJECTED):
                if user32 is not None:
                    return user32.CallNextHookEx(0, nCode, wParam, lParam)
                return 0

            now = float(time.monotonic())
            if now <= float(_ALLOWED_MOVE_UNTIL):
                dx = abs(int(info.pt.x) - int(_ALLOWED_MOVE_X))
                dy = abs(int(info.pt.y) - int(_ALLOWED_MOVE_Y))
                if dx <= 3 and dy <= 3:
                    if user32 is not None:
                        return user32.CallNextHookEx(0, nCode, wParam, lParam)
                    return 0
        except Exception:
            if user32 is not None:
                try:
                    return user32.CallNextHookEx(0, nCode, wParam, lParam)
                except Exception:
                    return 0
            return 0

        return 1


_USER_MOUSE_BLOCKER: Optional[_UserMouseMoveBlocker] = None
_USER_MOUSE_BLOCKER_LOCK = threading.Lock()


def shutdown_user_mouse_blocker(timeout_s: float = 2.0) -> None:
    blocker: Optional[_UserMouseMoveBlocker]
    with _USER_MOUSE_BLOCKER_LOCK:
        blocker = _USER_MOUSE_BLOCKER
    if blocker is not None:
        try:
            blocker.shutdown(timeout_s=timeout_s)
        except Exception:
            pass


atexit.register(shutdown_user_mouse_blocker)


@contextmanager
def _block_user_mouse_movement_during_actions(
    enabled: bool,
    *,
    log_fn: Optional[Callable[[str], None]] = None,
    notify_fn: Optional[Callable[[bool], None]] = None,
):
    if not bool(enabled):
        yield
        return

    global _USER_MOUSE_BLOCKER
    with _USER_MOUSE_BLOCKER_LOCK:
        if _USER_MOUSE_BLOCKER is None:
            _USER_MOUSE_BLOCKER = _UserMouseMoveBlocker()
        blocker = _USER_MOUSE_BLOCKER

    acquired = False
    try:
        acquired = bool(blocker.acquire(log_fn=log_fn))
        if acquired:
            if notify_fn is not None:
                try:
                    notify_fn(True)
                except Exception:
                    pass
            else:
                _auto_action_mouse_block_tooltip(True)
        yield
    finally:
        if acquired:
            if notify_fn is not None:
                try:
                    notify_fn(False)
                except Exception:
                    pass
            else:
                _auto_action_mouse_block_tooltip(False)
        if acquired:
            try:
                blocker.release()
            except Exception:
                pass


def _mouse_focus_wiggle(hwnd: int, x: int, y: int) -> None:
    """
    Tiny pre-click wiggle to help the game/window pick up the cursor and focus.
    """
    if autoit is None:
        return

    x = int(x)
    y = int(y)

    bounds = _client_origin_and_size(hwnd) if hwnd else None
    if bounds:
        left, top, width, height = bounds
        min_x = int(left)
        min_y = int(top)
        max_x = int(left + max(1, int(width)) - 1)
        max_y = int(top + max(1, int(height)) - 1)

        def _clamp(px: int, py: int) -> Tuple[int, int]:
            return (int(max(min_x, min(max_x, int(px)))), int(max(min_y, min(max_y, int(py)))))

    else:

        def _clamp(px: int, py: int) -> Tuple[int, int]:
            return int(px), int(py)

    dx, dy = 2, 1
    seq = [(x + dx, y + dy), (x - dx, y - dy), (x, y)]
    try:
        for i, (px, py) in enumerate(seq):
            cx, cy = _clamp(px, py)
            _note_program_mouse_target(int(cx), int(cy))
            autoit.mouse_move(int(cx), int(cy), speed=0)
            if i < len(seq) - 1:
                time.sleep(0)
    except Exception:
        pass

    try:
        _note_program_mouse_target(int(x), int(y))
        autoit.mouse_move(int(x), int(y), speed=0)
    except Exception:
        pass


def _mouse_left_click(hwnd: int, x: int, y: int) -> None:
    x = int(x)
    y = int(y)

    if autoit is None:
        return

    try:
        if hwnd and win32gui.IsWindow(hwnd) and win32gui.GetForegroundWindow() != hwnd:
            _bring_window_foreground(hwnd)
            time.sleep(0.01)
    except Exception:
        pass

    def _force_left_up() -> None:
        try:
            ctypes.windll.user32.mouse_event(int(win32con.MOUSEEVENTF_LEFTUP), 0, 0, 0, 0)
        except Exception:
            pass

    _mouse_move_instant(x, y)
    _mouse_focus_wiggle(hwnd, x, y)

    try:
        autoit.mouse_down("left")
        time.sleep(0.01)
    except Exception:
        pass
    finally:
        try:
            autoit.mouse_up("left")
        except Exception:
            pass
        _force_left_up()


def _send_ctrl_a() -> None:
    if autoit is not None:
        try:
            autoit.send("^a")
        except Exception:
            pass


def _auto_action_mouse_block_tooltip(show: bool) -> None:
    if autoit is None:
        return
    try:
        if not show:
            autoit.tooltip("")
            return

        try:
            cx, cy = win32api.GetCursorPos()
            x = int(cx) + 16
            y = int(cy) + 16
        except Exception:
            x = 10
            y = 10
        autoit.tooltip("User mouse movement is disabled during Auto-Actions.", int(x), int(y))
    except Exception:
        pass


_AUTO_ACTION_CLIPBOARD_LOCK = threading.RLock()
_AUTO_ACTION_CLIPBOARD_SCOPE = threading.local()
_AUTO_ACTION_CLIPBOARD_RESTORE_DELAY_S = 0.35


def _auto_action_clip_get_safe(*, buf_size: int = 256) -> Optional[str]:
    if autoit is None:
        return None
    try:
        return autoit.clip_get(buf_size=max(256, int(buf_size)))
    except Exception:
        return None


def _auto_action_clip_put_wait(value: str, *, timeout_s: float = 0.25) -> bool:
    if autoit is None:
        return False
    try:
        autoit.clip_put(value)
    except Exception:
        return False

    deadline = time.monotonic() + float(timeout_s or 0.0)
    buf_size = max(256, len(value) + 1)
    while time.monotonic() < deadline:
        if _auto_action_clip_get_safe(buf_size=buf_size) == value:
            return True
        time.sleep(0.01)
    return _auto_action_clip_get_safe(buf_size=buf_size) == value


@contextmanager
def _preserve_clipboard_during_auto_action_sequence(enabled: bool):
    """
    Preserve clipboard text once around a complete Auto-Actions sequence.

    Individual paste steps must leave their value available because Ctrl+V is
    consumed asynchronously by the target window. Restoring after each step
    can therefore make a later paste read the preceding value on slower hosts.
    """
    if autoit is None or not bool(enabled):
        yield
        return

    with _AUTO_ACTION_CLIPBOARD_LOCK:
        original = _auto_action_clip_get_safe(buf_size=65536)
        previous_scope = getattr(_AUTO_ACTION_CLIPBOARD_SCOPE, "state", None)
        state = {"changed": False}
        _AUTO_ACTION_CLIPBOARD_SCOPE.state = state
        try:
            yield
        finally:
            try:
                if bool(state["changed"]) and original is not None:
                    # Give the final Ctrl+V time to consume its value before
                    # restoring the clipboard captured at sequence start.
                    time.sleep(_AUTO_ACTION_CLIPBOARD_RESTORE_DELAY_S)
                    _auto_action_clip_put_wait(original, timeout_s=0.35)
            finally:
                if previous_scope is None:
                    try:
                        del _AUTO_ACTION_CLIPBOARD_SCOPE.state
                    except AttributeError:
                        pass
                else:
                    _AUTO_ACTION_CLIPBOARD_SCOPE.state = previous_scope


def _send_unicode_text(text: str) -> None:
    """
    Paste text via AutoIt, leaving clipboard restoration to the sequence scope.
    """
    if autoit is None:
        return

    s = str(text or "")

    with _AUTO_ACTION_CLIPBOARD_LOCK:
        try:
            if not _auto_action_clip_put_wait(s, timeout_s=0.35):
                raise RuntimeError("clipboard put did not stick")

            scope = getattr(_AUTO_ACTION_CLIPBOARD_SCOPE, "state", None)
            if scope is not None:
                scope["changed"] = True

            time.sleep(0.02)
            autoit.send("^v")
            time.sleep(0.12)
        except Exception:
            try:
                autoit.send(s, mode=1)
            except Exception:
                pass


def _bring_window_foreground(hwnd: int) -> bool:
    """
    Best-effort foreground activation for the given window.
    """
    try:
        if not win32gui.IsWindow(hwnd):
            return False

        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        def _toggle_topmost() -> None:
            try:
                flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                was_topmost = _is_window_topmost(hwnd)
                win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
                if not was_topmost:
                    try:
                        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
                    except Exception:
                        pass
            except Exception:
                pass

        current_thread_id: Optional[int] = None
        window_thread_id: Optional[int] = None
        try:
            current_thread_id = win32api.GetCurrentThreadId()
            window_thread_id = win32process.GetWindowThreadProcessId(hwnd)[0]
            ctypes.windll.user32.AttachThreadInput(current_thread_id, window_thread_id, True)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
        finally:
            if current_thread_id is not None and window_thread_id is not None:
                try:
                    ctypes.windll.user32.AttachThreadInput(current_thread_id, window_thread_id, False)
                except Exception:
                    pass

        try:
            if win32gui.GetForegroundWindow() != hwnd:
                _toggle_topmost()
                try:
                    current_thread_id = win32api.GetCurrentThreadId()
                    window_thread_id = win32process.GetWindowThreadProcessId(hwnd)[0]
                    ctypes.windll.user32.AttachThreadInput(current_thread_id, window_thread_id, True)
                    win32gui.BringWindowToTop(hwnd)
                    win32gui.SetForegroundWindow(hwnd)
                finally:
                    try:
                        if current_thread_id is not None and window_thread_id is not None:
                            ctypes.windll.user32.AttachThreadInput(current_thread_id, window_thread_id, False)
                    except Exception:
                        pass
        except Exception:
            pass

        return True
    except Exception:
        return False


def _is_window_topmost(hwnd: int) -> bool:
    try:
        exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        return bool(int(exstyle) & int(win32con.WS_EX_TOPMOST))
    except Exception:
        return False


def _set_window_topmost(hwnd: int, topmost: bool) -> None:
    try:
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
        insert_after = win32con.HWND_TOPMOST if bool(topmost) else win32con.HWND_NOTOPMOST
        win32gui.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, flags)
    except Exception:
        pass


@contextmanager
def _window_topmost_during(hwnd: int):
    original = _is_window_topmost(hwnd)
    _set_window_topmost(hwnd, True)
    try:
        yield
    finally:
        _set_window_topmost(hwnd, original)


def _screen_pixel_rgb(x: int, y: int) -> Optional[Tuple[int, int, int]]:
    if ImageGrab is None:
        return None
    try:
        img = ImageGrab.grab(bbox=(int(x), int(y), int(x) + 1, int(y) + 1))
        return tuple(img.getpixel((0, 0))[:3])  # type: ignore[return-value]
    except Exception:
        return None

try:
    from ocr_worker import capture_window_image as _capture_window_image
except Exception:  # pragma: no cover
    _capture_window_image = None  # type: ignore[assignment]


try:
    from auto_action_alerts import (
        AutoActionAlertRequest as _AutoActionAlertRequest,
        AutoActionAlertService as _AutoActionAlertService,
        MAX_PENDING_PRE_SEQUENCE_ALERTS as _MAX_PENDING_PRE_SEQUENCE_ALERTS,
        PRE_SEQUENCE_ALERT_LOOKAHEAD_S as _PRE_SEQUENCE_ALERT_LOOKAHEAD_S,
    )
except Exception:  # pragma: no cover - Auto-Actions must run without alerts.
    _AutoActionAlertRequest = None  # type: ignore[assignment]
    _MAX_PENDING_PRE_SEQUENCE_ALERTS = 5
    _PRE_SEQUENCE_ALERT_LOOKAHEAD_S = 60.0

    class _AutoActionAlertService:  # type: ignore[no-redef]
        available = False

        def pre_sequence_alert_enabled(self, *, enabled: bool, webhook_url: str, lead_s: float) -> bool:
            return False

        def antiafk_delay_reason(self, overdue_within_provider: Optional[Callable[[float], bool]], lead_s: float) -> str:
            return ""

        def antiafk_overdue_reason(self, overdue_within_provider: Optional[Callable[[float], bool]]) -> str:
            return ""

        def send_pre_sequence_alert(self, request: Any) -> bool:
            return False

        def send_step_webhook(
            self,
            *,
            webhook_url: str,
            message: str,
            embed_title: str = "",
            embed_description: str = "",
            screenshot: Optional[bytes] = None,
        ) -> bool:
            return False


class _MenuGateBlocked(Exception):
    pass


_ANTIAFK_EXECUTION_GUARD_FLOOR_S = 30.0


def _normalize_user_filter_mode(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in {"blacklist", "blocklist", "exclude", "denylist", "deny"}:
        return "blacklist"
    return "whitelist"


@dataclass(frozen=True)
class RelRect:
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class ColorConditionCheck:
    point: RelPoint
    color_hex: str = "#FFFFFF"
    tolerance: int = 0


@dataclass(frozen=True)
class ActionCondition:
    enabled: bool = False
    kind: str = "color"
    point: Optional[RelPoint] = None
    roi: Optional[RelRect] = None
    color_hex: str = "#FFFFFF"
    tolerance: int = 0
    target_text: str = ""
    match_mode: str = "contains"
    case_sensitive: bool = False
    filter_ids: Tuple[str, ...] = ()
    color_filters: Tuple[Tuple[int, int, int, int], ...] = ()
    color_checks: Tuple[ColorConditionCheck, ...] = ()


@dataclass(frozen=True)
class ActionStep:
    name: str
    kind: str
    point: Optional[RelPoint] = None
    end_point: Optional[RelPoint] = None
    text: str = ""
    paste_per_user: bool = False
    paste_user_values: Tuple[Tuple[str, str], ...] = ()
    webhook_url: str = ""
    webhook_message: str = ""
    webhook_include_screenshot: bool = False
    webhook_screenshot_roi: Optional[RelRect] = None
    webhook_embed_enabled: bool = False
    webhook_embed_title: str = ""
    webhook_embed_description: str = ""
    key: str = ""
    keys: Tuple[str, ...] = ()
    key_hold_s: float = 0.0
    click_button: str = "left"
    click_count: int = 1
    loop_count: int = 1
    wait_s: float = 0.0
    drag_duration_s: float = 0.5
    color_hex: str = "#FFFFFF"
    tolerance: int = 0
    select_all: bool = True
    scroll_direction: str = "down"
    target_row_id: str = ""
    target_row_name: str = ""
    condition: ActionCondition = field(default_factory=ActionCondition)


@dataclass(frozen=True)
class ActionRule:
    row_id: str
    name: str
    actions: Tuple[ActionStep, ...]
    cooldown_s: float
    startup_delay_s: float
    allowed_biomes: Tuple[str, ...]
    trigger_type: str
    trigger_filter_ids: Tuple[str, ...]
    trigger_merchants: Tuple[str, ...]
    repeat_mode: str
    repeat_count: int
    enabled: bool = True
    alert_enabled: bool = False
    alert_lead_s: float = 15.0
    alert_webhook: str = ""
    alert_message: str = ""


def _unique_strings(values: Iterable[Any], *, upper: bool = False) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in values or []:
        value = str(raw or "").strip()
        if not value:
            continue
        if upper:
            value = value.upper()
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _normalize_filter_ids(raw: Any) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple, set)):
        try:
            raw = list(raw)  # type: ignore[arg-type]
        except Exception:
            return ()
    return tuple(_unique_strings(raw))


_AUTO_ACTION_MERCHANTS = ("Jester", "Mari", "Rin")


def _normalize_merchants(raw: Any) -> Tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return ()
    by_name = {name.lower(): name for name in _AUTO_ACTION_MERCHANTS}
    out: List[str] = []
    for value in raw:
        merchant = by_name.get(str(value or "").strip().lower())
        if merchant and merchant not in out:
            out.append(merchant)
    return tuple(out)


def _normalize_user_text_map(raw: Any) -> Tuple[Tuple[str, str], ...]:
    pairs: List[Tuple[Any, Any]] = []
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                uid = item.get("uid", item.get("user_id", item.get("id", item.get("user"))))
                if "text" in item:
                    text = item.get("text")
                elif "value" in item:
                    text = item.get("value")
                else:
                    text = item.get("paste", item.get("content", ""))
                pairs.append((uid, text))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                pairs.append((item[0], item[1]))

    out: Dict[str, str] = {}
    for raw_uid, raw_text in pairs:
        uid = str(raw_uid or "").strip()
        if not uid:
            continue
        out[uid] = str(raw_text if raw_text is not None else "")
    return tuple((uid, out[uid]) for uid in sorted(out.keys()))


def _normalize_condition_color_filters(raw: Any) -> Tuple[Tuple[int, int, int, int], ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    out: List[Tuple[int, int, int, int]] = []
    seen: set[Tuple[int, int, int, int]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        if not bool(item.get("enabled", True)):
            continue
        try:
            spec = (
                max(0, min(255, int(item.get("r", 255) or 0))),
                max(0, min(255, int(item.get("g", 255) or 0))),
                max(0, min(255, int(item.get("b", 255) or 0))),
                max(0, min(255, int(item.get("tol", item.get("tolerance", 40)) or 0))),
            )
        except Exception:
            continue
        if spec in seen:
            continue
        seen.add(spec)
        out.append(spec)
    return tuple(out)


def _normalize_rel_point(raw: Any) -> Optional[RelPoint]:
    if not isinstance(raw, dict):
        return None
    try:
        return RelPoint(float(raw.get("x", 0.0)), float(raw.get("y", 0.0)))
    except Exception:
        return None


def _normalize_rel_rect(raw: Any) -> Optional[RelRect]:
    if not isinstance(raw, dict):
        return None
    try:
        rect = RelRect(
            float(raw.get("x", 0.0)),
            float(raw.get("y", 0.0)),
            float(raw.get("w", raw.get("width", 0.0))),
            float(raw.get("h", raw.get("height", 0.0))),
        )
    except Exception:
        return None
    if rect.w <= 0.0 or rect.h <= 0.0:
        return None
    return rect


def _normalize_condition_color_checks(
    raw: Any,
    *,
    default_tolerance: int = 0,
) -> Tuple[ColorConditionCheck, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    out: List[ColorConditionCheck] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        point = _normalize_rel_point(item.get("point") or item.get("condition_point"))
        if point is None and "x" in item and "y" in item:
            point = _normalize_rel_point(item)
        if point is None:
            continue
        try:
            tolerance = max(
                0,
                min(
                    255,
                    int(item.get("tolerance", item.get("tol", default_tolerance)) or 0),
                ),
            )
        except Exception:
            tolerance = 0
        out.append(
            ColorConditionCheck(
                point=point,
                color_hex=str(item.get("color") or item.get("color_hex") or "#FFFFFF").strip() or "#FFFFFF",
                tolerance=tolerance,
            )
        )
    return tuple(out)


def _normalize_condition(
    raw: Any,
    *,
    default_enabled: bool = False,
    legacy_point: Optional[RelPoint] = None,
    legacy_color: str = "#FFFFFF",
    legacy_tolerance: int = 0,
) -> ActionCondition:
    base = raw if isinstance(raw, dict) else {}

    kind = str(base.get("type") or base.get("kind") or base.get("condition_type") or "color").strip().lower()
    if kind in ("colour", "pixel", "pixel_color", "color_conditional", "conditional_click"):
        kind = "color"
    elif kind in ("ocr", "ocr_text", "ocr_conditional"):
        kind = "ocr"
    else:
        kind = "color"

    enabled_default = bool(default_enabled)
    enabled = bool(base.get("enabled", enabled_default))

    try:
        tolerance = max(0, int(base.get("tolerance", base.get("tol", legacy_tolerance)) or 0))
    except Exception:
        tolerance = max(0, int(legacy_tolerance or 0))

    point = _normalize_rel_point(base.get("point") or base.get("condition_point") or base.get("conditional_point"))
    if point is None:
        point = legacy_point

    color_hex = str(base.get("color") or base.get("color_hex") or legacy_color or "#FFFFFF").strip() or "#FFFFFF"
    color_check_keys = ("color_checks", "color_pairs", "point_color_pairs")
    explicit_color_checks = next((base.get(key) for key in color_check_keys if key in base), None)
    has_explicit_color_checks = any(key in base for key in color_check_keys)
    color_checks = (
        _normalize_condition_color_checks(explicit_color_checks, default_tolerance=tolerance)
        if has_explicit_color_checks
        else ()
    )
    if not has_explicit_color_checks and point is not None:
        color_checks = (
            ColorConditionCheck(point=point, color_hex=color_hex, tolerance=tolerance),
        )

    roi = _normalize_rel_rect(base.get("roi") or base.get("area") or base.get("ocr_roi"))

    match_mode = str(base.get("match_mode") or base.get("ocr_match_mode") or "contains").strip().lower()
    if match_mode not in ("contains", "equals", "regex", "fuzzy"):
        match_mode = "contains"

    return ActionCondition(
        enabled=enabled,
        kind=kind,
        point=point,
        roi=roi,
        color_hex=color_hex,
        tolerance=tolerance,
        color_checks=color_checks,
        target_text=str(base.get("target_text") or base.get("ocr_text") or base.get("text") or "").strip(),
        match_mode=match_mode,
        case_sensitive=bool(base.get("case_sensitive", False)),
        filter_ids=_normalize_filter_ids(base.get("filter_ids", base.get("ocr_filter_ids", [])) or []),
        color_filters=_normalize_condition_color_filters(
            base.get("color_filters", base.get("ocr_color_filters", [])) or []
        ),
    )


def _normalize_action_step(raw: Any, *, fallback_name: str = "") -> Optional[ActionStep]:
    if not isinstance(raw, dict):
        return None

    kind = str(raw.get("type") or raw.get("kind") or raw.get("action_type") or "").strip().lower()
    legacy_conditional_click = kind in ("conditional", "conditional_click")
    if legacy_conditional_click:
        kind = "click"
    elif kind in ("keyboard", "keyboard_click", "key_click", "keypress", "key_press"):
        kind = "key"
    elif kind in ("end", "end_if", "endif", "end_loop", "end_block"):
        kind = "end"
    elif kind in ("sleep", "wait_sleep", "delay"):
        kind = "wait"
    elif kind in ("mouse_drag", "drag_click", "click_drag"):
        kind = "drag"
    elif kind in ("discord_webhook", "send_webhook", "webhook_send"):
        kind = "webhook"
    elif kind in (
        "action_sequence",
        "play_action",
        "play_action_row",
        "trigger_action",
        "trigger_action_row",
    ):
        kind = "action_row"
    elif kind in (
        "kill",
        "kill_user",
        "kill_user_processes",
        "terminate_user",
        "restart",
        "restart_user",
        "restart_user_session",
        "relaunch",
        "relaunch_user",
    ):
        kind = "kill_user"
    if kind not in (
        "click",
        "key",
        "paste",
        "scroll",
        "drag",
        "wait",
        "webhook",
        "action_row",
        "kill_user",
        "if",
        "else",
        "end",
        "break",
        "loop",
    ):
        return None

    name = str(raw.get("name") or fallback_name or kind.replace("_", " ").title()).strip()
    point = _normalize_rel_point(raw.get("point") or raw.get("start_point") or raw.get("from_point"))
    end_point = _normalize_rel_point(raw.get("end_point") or raw.get("to_point") or raw.get("target_point"))

    try:
        tolerance = max(0, int(raw.get("tolerance", 0) or 0))
    except Exception:
        tolerance = 0

    try:
        select_all = bool(raw.get("select_all", True))
    except Exception:
        select_all = True

    paste_per_user = bool(
        raw.get(
            "paste_per_user",
            raw.get("per_user_paste", raw.get("user_specific_paste", False)),
        )
    )
    paste_user_values = _normalize_user_text_map(
        raw.get(
            "paste_user_values",
            raw.get(
                "per_user_paste_values",
                raw.get("user_texts", raw.get("per_user_text", raw.get("user_values", {}))),
            ),
        )
    )

    scroll_direction = "up" if str(raw.get("scroll_direction") or raw.get("direction") or "").strip().lower() == "up" else "down"

    click_button = str(raw.get("click_button") or raw.get("button") or "left").strip().lower()
    if click_button not in ("left", "right"):
        click_button = "left"

    try:
        click_count = max(1, min(2, int(raw.get("click_count", 2 if bool(raw.get("double_click", False)) else 1) or 1)))
    except Exception:
        click_count = 1

    try:
        key_hold_s = max(0.0, float(raw.get("key_hold_s", raw.get("hold_s", raw.get("hold_seconds", 0.0))) or 0.0))
    except Exception:
        key_hold_s = 0.0
    key_values = _unique_strings(raw.get("keys") or [])
    legacy_key = str(raw.get("key") or raw.get("key_name") or "").strip()
    if legacy_key and legacy_key not in key_values:
        key_values.insert(0, legacy_key)

    try:
        loop_count = max(1, int(raw.get("loop_count", raw.get("count", raw.get("times", 1))) or 1))
    except Exception:
        loop_count = 1

    try:
        wait_s = max(0.0, float(raw.get("wait_s", raw.get("sleep_s", raw.get("seconds", raw.get("duration", 0.0)))) or 0.0))
    except Exception:
        wait_s = 0.0

    try:
        drag_duration_s = max(0.0, float(raw.get("drag_duration_s", raw.get("drag_seconds", raw.get("move_duration_s", 0.5))) or 0.0))
    except Exception:
        drag_duration_s = 0.5

    condition_raw = raw.get("condition") if isinstance(raw.get("condition"), dict) else raw.get("conditional")
    if legacy_conditional_click:
        condition = _normalize_condition(
            condition_raw,
            default_enabled=True,
            legacy_point=point,
            legacy_color=str(raw.get("color") or raw.get("color_hex") or "#FFFFFF").strip() or "#FFFFFF",
            legacy_tolerance=tolerance,
        )
    else:
        top_level_condition_keys = (
            "condition_enabled",
            "conditional_enabled",
            "condition_type",
            "condition_point",
            "conditional_point",
            "condition_color_checks",
            "color_checks",
            "ocr_text",
            "target_text",
            "ocr_roi",
        )
        if not isinstance(condition_raw, dict) and any(k in raw for k in top_level_condition_keys):
            condition_raw = {
                "enabled": raw.get("condition_enabled", raw.get("conditional_enabled", False)),
                "type": raw.get("condition_type", raw.get("conditional_type", "color")),
                "point": raw.get("condition_point") or raw.get("conditional_point"),
                "roi": raw.get("ocr_roi") or raw.get("roi"),
                "color": raw.get("condition_color", raw.get("color", "#FFFFFF")),
                "tolerance": raw.get("condition_tolerance", raw.get("tolerance", 0)),
                "target_text": raw.get("target_text", raw.get("ocr_text", "")),
                "match_mode": raw.get("match_mode", raw.get("ocr_match_mode", "contains")),
                "case_sensitive": raw.get("case_sensitive", False),
            }
            if "condition_color_checks" in raw or "color_checks" in raw:
                condition_raw["color_checks"] = raw.get("condition_color_checks", raw.get("color_checks"))
        condition = _normalize_condition(condition_raw, default_enabled=(kind == "if"))

    return ActionStep(
        name=name or kind.replace("_", " ").title(),
        kind=kind,
        point=point,
        end_point=end_point,
        text=str(raw.get("text") or ""),
        paste_per_user=paste_per_user,
        paste_user_values=paste_user_values,
        webhook_url=str(raw.get("webhook_url") or raw.get("url") or "").strip(),
        webhook_message=str(
            raw.get("webhook_message")
            or raw.get("message")
            or raw.get("content")
            or (raw.get("text") if kind == "webhook" else "")
            or ""
        ),
        webhook_include_screenshot=bool(
            raw.get("webhook_include_screenshot", raw.get("include_screenshot", False))
        ),
        webhook_screenshot_roi=_normalize_rel_rect(
            raw.get("webhook_screenshot_roi") or raw.get("screenshot_roi")
        ),
        webhook_embed_enabled=bool(
            raw.get("webhook_embed_enabled", raw.get("embed_enabled", False))
        ),
        webhook_embed_title=str(raw.get("webhook_embed_title") or raw.get("embed_title") or ""),
        webhook_embed_description=str(
            raw.get("webhook_embed_description") or raw.get("embed_description") or ""
        ),
        key=key_values[0] if key_values else "",
        keys=tuple(key_values),
        key_hold_s=key_hold_s,
        click_button=click_button,
        click_count=click_count,
        loop_count=loop_count,
        wait_s=wait_s,
        drag_duration_s=drag_duration_s,
        color_hex=str(raw.get("color") or raw.get("color_hex") or "#FFFFFF").strip() or "#FFFFFF",
        tolerance=tolerance,
        select_all=select_all,
        scroll_direction=scroll_direction,
        target_row_id=str(
            raw.get("target_row_id")
            or raw.get("target_action_row_id")
            or raw.get("target_action_id")
            or ""
        ).strip(),
        target_row_name=str(
            raw.get("target_row_name")
            or raw.get("target_action_row_name")
            or raw.get("target_action_name")
            or ""
        ).strip(),
        condition=condition,
    )


def _normalize_behavior(raw: Any, legacy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = raw if isinstance(raw, dict) else {}
    legacy = legacy or {}

    trigger = base.get("trigger") if isinstance(base.get("trigger"), dict) else {}
    filter_ids = _normalize_filter_ids(
        trigger.get("filter_ids", base.get("filter_ids", legacy.get("filter_ids", []))) or []
    )
    merchants = _normalize_merchants(
        trigger.get(
            "merchants",
            trigger.get(
                "merchant_names",
                base.get("merchants", legacy.get("merchants", legacy.get("merchant_names", []))),
            ),
        )
        or []
    )
    raw_trigger_type = str(trigger.get("type") or base.get("trigger_type") or "").strip().lower()
    if raw_trigger_type in ("normal", "none"):
        trigger_type = "normal"
    elif raw_trigger_type == "ocr_filter":
        trigger_type = "ocr_filter" if filter_ids else "normal"
    elif raw_trigger_type == "merchant":
        trigger_type = "merchant" if merchants else "normal"
    elif raw_trigger_type in ("action_row", "action_sequence", "action_step"):
        trigger_type = "action_row"
    else:
        trigger_type = "ocr_filter" if filter_ids else ("merchant" if merchants else "normal")
    if trigger_type != "ocr_filter":
        filter_ids = ()
    if trigger_type != "merchant":
        merchants = ()

    repeat_mode = str(base.get("repeat_mode") or legacy.get("repeat_mode") or "repeat").strip().lower()
    if repeat_mode not in ("repeat", "count", "count_per_trigger", "once_per_pid"):
        repeat_mode = "repeat"

    try:
        repeat_count = max(1, int(base.get("repeat_count", legacy.get("repeat_count", 1)) or 1))
    except Exception:
        repeat_count = 1

    try:
        cooldown = max(
            0.0,
            float(
                base.get(
                    "cooldown",
                    legacy.get("cooldown", legacy.get("cooldown_s", 0.0)),
                )
                or 0.0
            ),
        )
    except Exception:
        cooldown = 0.0

    try:
        startup_delay = max(
            0.0,
            float(
                base.get(
                    "startup_delay",
                    base.get(
                        "startup_delay_s",
                        legacy.get("startup_delay", legacy.get("startup_delay_s", 10.0)),
                    ),
                )
            ),
        )
    except Exception:
        startup_delay = 10.0

    biomes = tuple(
        _unique_strings(
            base.get("biomes", legacy.get("biomes", legacy.get("allowed_biomes", []))) or [],
            upper=True,
        )
    )

    return {
        "cooldown": cooldown,
        "startup_delay": startup_delay,
        "biomes": biomes,
        "repeat_mode": repeat_mode,
        "repeat_count": repeat_count,
        "trigger_type": trigger_type,
        "filter_ids": filter_ids,
        "merchants": merchants,
    }


_AUTOIT_KEY_ALIASES = {
    "space": "{SPACE}",
    "enter": "{ENTER}",
    "return": "{ENTER}",
    "tab": "{TAB}",
    "esc": "{ESC}",
    "escape": "{ESC}",
    "backspace": "{BACKSPACE}",
    "delete": "{DELETE}",
    "del": "{DELETE}",
    "insert": "{INSERT}",
    "ins": "{INSERT}",
    "home": "{HOME}",
    "end": "{END}",
    "pageup": "{PGUP}",
    "pgup": "{PGUP}",
    "pagedown": "{PGDN}",
    "pgdn": "{PGDN}",
    "up": "{UP}",
    "down": "{DOWN}",
    "left": "{LEFT}",
    "right": "{RIGHT}",
    "shift": "{SHIFT}",
    "ctrl": "{CTRL}",
    "control": "{CTRL}",
    "alt": "{ALT}",
    "caps lock": "{CAPSLOCK}",
    "capslock": "{CAPSLOCK}",
    "num lock": "{NUMLOCK}",
    "numlock": "{NUMLOCK}",
    "scroll lock": "{SCROLLLOCK}",
    "scrolllock": "{SCROLLLOCK}",
    "print screen": "{PRINTSCREEN}",
    "printscreen": "{PRINTSCREEN}",
    "pause": "{PAUSE}",
    "break": "{BREAK}",
    "menu": "{APPSKEY}",
    "apps key": "{APPSKEY}",
    "left windows": "{LWIN}",
    "right windows": "{RWIN}",
    "left shift": "{LSHIFT}",
    "right shift": "{RSHIFT}",
    "left ctrl": "{LCTRL}",
    "right ctrl": "{RCTRL}",
    "left alt": "{LALT}",
    "right alt": "{RALT}",
    "numpad 0": "{NUMPAD0}",
    "numpad 1": "{NUMPAD1}",
    "numpad 2": "{NUMPAD2}",
    "numpad 3": "{NUMPAD3}",
    "numpad 4": "{NUMPAD4}",
    "numpad 5": "{NUMPAD5}",
    "numpad 6": "{NUMPAD6}",
    "numpad 7": "{NUMPAD7}",
    "numpad 8": "{NUMPAD8}",
    "numpad 9": "{NUMPAD9}",
    "numpad multiply": "{NUMPADMULT}",
    "numpad add": "{NUMPADADD}",
    "numpad subtract": "{NUMPADSUB}",
    "numpad divide": "{NUMPADDIV}",
    "numpad period": "{NUMPADDOT}",
    "numpad enter": "{NUMPADENTER}",
    "backtick (`)": "`",
    "minus (-)": "-",
    "equals (=)": "=",
    "left bracket ([)": "[",
    "right bracket (])": "]",
    "backslash (\\)": "\\",
    "semicolon (;)": ";",
    "apostrophe (')": "'",
    "comma (,)": ",",
    "period (.)": ".",
    "slash (/)": "/",
}


def _autoit_key_token(key: str) -> str:
    raw = str(key or "").strip()
    if not raw:
        return ""
    if raw.startswith("{") and raw.endswith("}"):
        return raw
    low = raw.lower()
    if low in _AUTOIT_KEY_ALIASES:
        return _AUTOIT_KEY_ALIASES[low]
    if re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", low):
        return "{" + low.upper() + "}"
    if len(raw) == 1:
        return raw
    return "{" + raw.upper() + "}"


def _autoit_key_hold_tokens(key: str) -> Tuple[str, str]:
    token = _autoit_key_token(key)
    if not token:
        return "", ""
    if token.startswith("{") and token.endswith("}"):
        name = token[1:-1].strip()
    else:
        name = token.strip()
    if not name:
        return "", ""
    return "{" + name + " down}", "{" + name + " up}"


def _send_single_key(key: str, hold_s: float = 0.0) -> None:
    _send_keys([key], hold_s)


def _send_keys(keys: Sequence[str], hold_s: float = 0.0) -> None:
    if autoit is None:
        return
    clean_keys = _unique_strings(keys or [])
    if not clean_keys:
        return
    try:
        hold = max(0.0, float(hold_s or 0.0))
    except Exception:
        hold = 0.0

    if len(clean_keys) > 1 or hold > 0.0:
        down_up = [_autoit_key_hold_tokens(key) for key in clean_keys]
        down_up = [(down, up) for down, up in down_up if down and up]
        if down_up:
            try:
                for down, _up in down_up:
                    autoit.send(down)
                    time.sleep(0.01)
                time.sleep(max(0.02, hold))
            finally:
                for _down, up in reversed(down_up):
                    try:
                        autoit.send(up)
                        time.sleep(0.005)
                    except Exception:
                        pass
            return

    token = _autoit_key_token(clean_keys[0])
    if not token:
        return
    try:
        if len(token) == 1:
            autoit.send(token, mode=1)
        else:
            autoit.send(token)
    except Exception:
        pass


def _mouse_button_click(hwnd: int, x: int, y: int, *, button: str = "left", count: int = 1) -> None:
    button_s = "right" if str(button or "").strip().lower() == "right" else "left"
    try:
        count_i = max(1, min(2, int(count or 1)))
    except Exception:
        count_i = 1

    for idx in range(count_i):
        if button_s == "left":
            _mouse_left_click(hwnd, int(x), int(y))
        else:
            try:
                if hwnd and _bring_window_foreground(hwnd):
                    time.sleep(0.01)
            except Exception:
                pass
            _mouse_move_instant(int(x), int(y))
            try:
                autoit.mouse_down("right")
                time.sleep(0.01)
            except Exception:
                pass
            finally:
                try:
                    autoit.mouse_up("right")
                except Exception:
                    pass
                try:
                    ctypes.windll.user32.mouse_event(int(win32con.MOUSEEVENTF_RIGHTUP), 0, 0, 0, 0)
                except Exception:
                    pass
        if idx + 1 < count_i:
            time.sleep(0.06)


def _mouse_button_drag(
    hwnd: int,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    *,
    button: str = "left",
    duration_s: float = 0.5,
) -> None:
    if autoit is None:
        return
    button_s = "right" if str(button or "").strip().lower() == "right" else "left"
    try:
        duration = max(0.0, float(duration_s or 0.0))
    except Exception:
        duration = 0.5
    try:
        if hwnd:
            _bring_window_foreground(hwnd)
            time.sleep(0.01)
    except Exception:
        pass

    _mouse_move_instant(int(start_x), int(start_y))
    time.sleep(0.02)

    up_flag = win32con.MOUSEEVENTF_RIGHTUP if button_s == "right" else win32con.MOUSEEVENTF_LEFTUP
    try:
        autoit.mouse_down(button_s)
        if duration <= 0.0:
            _mouse_move_instant(int(end_x), int(end_y))
        else:
            steps = max(2, min(240, int(duration * 60)))
            sleep_s = duration / float(steps)
            sx = float(start_x)
            sy = float(start_y)
            ex = float(end_x)
            ey = float(end_y)
            for step_idx in range(1, steps + 1):
                t = float(step_idx) / float(steps)
                x = int(round(sx + ((ex - sx) * t)))
                y = int(round(sy + ((ey - sy) * t)))
                _mouse_move_instant(x, y)
                time.sleep(max(0.001, sleep_s))
    except Exception:
        pass
    finally:
        try:
            autoit.mouse_up(button_s)
        except Exception:
            pass
        try:
            ctypes.windll.user32.mouse_event(int(up_flag), 0, 0, 0, 0)
        except Exception:
            pass


def _window_rel_pixel_rgb(hwnd: int, point: RelPoint) -> Optional[Tuple[int, int, int]]:
    if _capture_window_image is None:
        return None
    try:
        full = _capture_window_image(int(hwnd))
    except Exception:
        full = None
    if full is None:
        return None
    try:
        try:
            _lo, hi = full.convert("L").getextrema()
            if int(hi) <= 5:
                return None
        except Exception:
            pass
        _cl, _ct, cr, cb = win32gui.GetClientRect(int(hwnd))
        client_w = max(1, int(cr - _cl))
        client_h = max(1, int(cb - _ct))
        wl, wt, wr, wb = win32gui.GetWindowRect(int(hwnd))
        win_w = max(1, int(wr - wl))
        win_h = max(1, int(wb - wt))
        scale_x = float(full.width) / float(win_w)
        scale_y = float(full.height) / float(win_h)
        client_left, client_top = win32gui.ClientToScreen(int(hwnd), (0, 0))
        crop_left = int((client_left - wl) * scale_x)
        crop_top = int((client_top - wt) * scale_y)
        crop_w = max(1, int(client_w * scale_x))
        crop_h = max(1, int(client_h * scale_y))
        rel_x = max(0.0, min(1.0, float(point.x)))
        rel_y = max(0.0, min(1.0, float(point.y)))
        px = max(0, min(full.width - 1, crop_left + int(rel_x * crop_w)))
        py = max(0, min(full.height - 1, crop_top + int(rel_y * crop_h)))
        return tuple(full.convert("RGB").getpixel((px, py))[:3])  # type: ignore[return-value]
    except Exception:
        return None


def _condition_payload(condition: ActionCondition) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "enabled": bool(condition.enabled),
        "type": str(condition.kind or "color"),
        "color": str(condition.color_hex or "#FFFFFF"),
        "tolerance": int(condition.tolerance or 0),
        "color_checks": [
            {
                "point": {"x": float(check.point.x), "y": float(check.point.y)},
                "color": str(check.color_hex or "#FFFFFF"),
                "tolerance": int(check.tolerance or 0),
            }
            for check in (condition.color_checks or ())
        ],
        "target_text": str(condition.target_text or ""),
        "match_mode": str(condition.match_mode or "contains"),
        "case_sensitive": bool(condition.case_sensitive),
        "filter_ids": list(condition.filter_ids or ()),
        "color_filters": [
            {"r": int(r), "g": int(g), "b": int(b), "tol": int(tol), "enabled": True}
            for r, g, b, tol in (condition.color_filters or ())
        ],
    }
    if condition.point is not None:
        payload["point"] = {"x": float(condition.point.x), "y": float(condition.point.y)}
    if condition.roi is not None:
        payload["roi"] = {
            "x": float(condition.roi.x),
            "y": float(condition.roi.y),
            "w": float(condition.roi.w),
            "h": float(condition.roi.h),
        }
    return payload


def _ocr_text_matches(text: str, condition: ActionCondition) -> bool:
    raw_text = str(text or "")
    target = str(condition.target_text or "").strip()
    if not target:
        return bool(raw_text.strip())

    mode = str(condition.match_mode or "contains")
    flags = 0 if bool(condition.case_sensitive) else re.IGNORECASE
    if mode == "regex":
        try:
            return re.search(target, raw_text, flags=flags) is not None
        except re.error:
            return False

    if bool(condition.case_sensitive):
        haystack = raw_text
        needle = target
    else:
        haystack = raw_text.lower()
        needle = target.lower()

    if str(condition.match_mode or "contains") == "equals":
        lines = [line.strip() for line in haystack.splitlines() if line.strip()]
        return haystack.strip() == needle.strip() or needle.strip() in lines

    if mode == "fuzzy":
        def _normalize(value: str) -> str:
            return re.sub(r"\s+", " ", str(value or "")).strip()

        hay_norm = _normalize(haystack)
        needle_norm = _normalize(needle)
        if not needle_norm:
            return bool(hay_norm)
        if needle_norm in hay_norm:
            return True

        threshold = 0.9 if len(needle_norm) < 16 else 0.82
        candidates = [hay_norm]
        candidates.extend(_normalize(line) for line in haystack.splitlines() if _normalize(line))

        words = hay_norm.split()
        target_words = needle_norm.split()
        if words and target_words:
            target_count = len(target_words)
            for size in range(max(1, target_count - 2), min(len(words), target_count + 2) + 1):
                for start in range(0, max(0, len(words) - size) + 1):
                    candidates.append(" ".join(words[start : start + size]))

        seen: set[str] = set()
        for candidate in candidates:
            candidate = _normalize(candidate)
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            if difflib.SequenceMatcher(None, candidate, needle_norm).ratio() >= threshold:
                return True
        return False

    return needle in haystack


class AutoActionEngine:
    """
    Background worker that plays configured action strings for selected users.

    The host is expected to:
      - Call `update_config()` whenever UI settings change.
      - Provide `record_ocr_filter_trigger()` events from the OCR worker.
      - Provide `record_merchant_trigger()` events from merchant detection.
      - Provide pid/biome/menu/window lookup callbacks for each user.
    """

    def __init__(
        self,
        *,
        pid_provider: Callable[[str], Optional[int]],
        hwnd_provider: Callable[[int], Optional[int]],
        biome_provider: Callable[[str], str],
        in_menu_provider: Optional[Callable[[str], Optional[bool]]] = None,
        username_provider: Optional[Callable[[str], str]] = None,
        user_ids_provider: Optional[Callable[[], Iterable[str]]] = None,
        log_filename_provider: Optional[Callable[[str], str]] = None,
        server_label_provider: Optional[Callable[[str], str]] = None,
        ps_link_provider: Optional[Callable[[str], str]] = None,
        discord_ping_provider: Optional[Callable[[str], str]] = None,
        log: Callable[[str], None],
        mouse_block_notify: Optional[Callable[[bool], None]] = None,
        pause_antiafk: Optional[Callable[[], None]] = None,
        resume_antiafk: Optional[Callable[[], None]] = None,
        antiafk_overdue_within_provider: Optional[Callable[[float], bool]] = None,
        pre_action_hook: Optional[Callable[[str, int], float]] = None,
        post_action_hook: Optional[Callable[[str, int], None]] = None,
        ocr_text_provider: Optional[Callable[[int, Dict[str, Any]], str]] = None,
        kill_user_callback: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self._pid_provider = pid_provider
        self._hwnd_provider = hwnd_provider
        self._biome_provider = biome_provider
        self._in_menu_provider = in_menu_provider
        self._username_provider = username_provider
        self._user_ids_provider = user_ids_provider
        self._log_filename_provider = log_filename_provider
        self._server_label_provider = server_label_provider
        self._ps_link_provider = ps_link_provider
        self._discord_ping_provider = discord_ping_provider
        self._log = log
        self._mouse_block_notify = mouse_block_notify
        self._pause_antiafk = pause_antiafk
        self._resume_antiafk = resume_antiafk
        self._antiafk_overdue_within_provider = antiafk_overdue_within_provider
        self._pre_action_hook = pre_action_hook
        self._post_action_hook = post_action_hook
        self._ocr_text_provider = ocr_text_provider
        self._kill_user_callback = kill_user_callback
        self._alert_service = _AutoActionAlertService()

        self._cfg_lock = threading.Lock()
        self._cfg: Dict[str, Any] = {"enabled": False}

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._action_lock = threading.Lock()

        self._state_lock = threading.Lock()
        self._next_ready: Dict[str, Dict[int, float]] = {}
        self._pending_use_at: Dict[str, Dict[int, float]] = {}
        self._pending_alert_send_at: Dict[str, Dict[int, float]] = {}
        self._pending_trigger_seq: Dict[str, Dict[int, int]] = {}
        self._last_trigger_seq: Dict[str, Dict[int, int]] = {}
        self._completed_pids: Dict[str, Dict[int, set[int]]] = {}
        self._completed_startup_rows: Dict[str, set[int]] = {}
        self._rule_signature: Dict[str, Dict[int, int]] = {}
        self._not_in_menu_since: Dict[Tuple[str, int], float] = {}
        self._trigger_events: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._merchant_events: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._trigger_seq: int = 0
        self._last_antiafk_alert_log_ts: float = 0.0
        self._last_alert_queue_limit_log_ts: float = 0.0
        self._last_alert_send_at: float = 0.0
        self._active_action: Optional[Tuple[str, int]] = None
        self._active_action_row_id: str = ""
        self._active_action_chain: Tuple[str, ...] = ()

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="AutoActionEngine", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout_s)))
        shutdown_user_mouse_blocker(timeout_s=1.0)

    def update_config(self, cfg: Dict[str, Any]) -> None:
        with self._cfg_lock:
            self._cfg = copy.deepcopy(cfg or {})

    def reset_manager_startup_counts(self) -> None:
        """Allow startup-limited rows to run again for a new manager session."""
        with self._state_lock:
            self._completed_startup_rows.clear()

    def monitor_snapshot(self) -> Dict[str, Any]:
        """Return a thread-safe, presentation-neutral snapshot for runtime monitors."""
        cfg = self._cfg_snapshot()
        now = time.time()
        users = self._users_from_cfg(cfg)
        # Parsing actions is comparatively expensive. Parse each configured row
        # once, then apply its user filter below instead of rebuilding every
        # ActionRule for every account in the monitor.
        all_rules = self._rules_from_cfg(cfg)
        raw_items = cfg.get("items") or []
        if not isinstance(raw_items, list):
            raw_items = []

        with self._state_lock:
            next_ready = copy.deepcopy(self._next_ready)
            pending_use = copy.deepcopy(self._pending_use_at)
            pending_send = copy.deepcopy(self._pending_alert_send_at)
            consumed = copy.deepcopy(self._last_trigger_seq)
            completed_pids = copy.deepcopy(self._completed_pids)
            completed_startup_rows = copy.deepcopy(self._completed_startup_rows)
            trigger_events = copy.deepcopy(self._trigger_events)
            merchant_events = copy.deepcopy(self._merchant_events)
            not_in_menu_since = dict(self._not_in_menu_since)
            active_action = self._active_action

        rows: List[Dict[str, Any]] = []
        for uid in users:
            applicable_rules = []
            for idx, rule in all_rules:
                raw_item = raw_items[idx] if 0 <= int(idx) < len(raw_items) else {}
                if self._item_applies_to_uid(raw_item, uid):
                    applicable_rules.append((idx, rule))
            if not applicable_rules:
                continue

            username = self._username(uid)
            try:
                pid = int(self._pid_provider(uid) or 0)
            except Exception:
                pid = 0
            try:
                biome = str(self._biome_provider(uid) or "").strip()
            except Exception:
                biome = ""
            try:
                in_menu = self._in_menu_provider(uid) if self._in_menu_provider is not None else None
            except Exception:
                in_menu = None

            for idx, rule in applicable_rules:
                idx_i = int(idx)
                ready_at = float((next_ready.get(uid) or {}).get(idx_i, 0.0) or 0.0)
                use_at = float((pending_use.get(uid) or {}).get(idx_i, 0.0) or 0.0)
                send_at_raw = (pending_send.get(uid) or {}).get(idx_i)
                send_at = float(send_at_raw or 0.0) if send_at_raw is not None else None
                is_completed = bool(
                    (
                        rule.repeat_mode == "count"
                        and idx_i in (completed_startup_rows.get(uid) or set())
                    )
                    or (
                        rule.repeat_mode == "once_per_pid"
                        and pid > 0
                        and pid in ((completed_pids.get(uid) or {}).get(idx_i, set()) or set())
                    )
                )

                latest_trigger: Optional[Dict[str, Any]] = None
                if rule.trigger_type == "ocr_filter":
                    user_events = trigger_events.get(uid) or {}
                    for filter_id in rule.trigger_filter_ids:
                        event = user_events.get(str(filter_id))
                        if event and (
                            latest_trigger is None
                            or int(event.get("seq", 0) or 0) > int(latest_trigger.get("seq", 0) or 0)
                        ):
                            latest_trigger = event
                elif rule.trigger_type == "merchant":
                    user_events = merchant_events.get(uid) or {}
                    for merchant in rule.trigger_merchants:
                        event = user_events.get(str(merchant))
                        if event and (
                            latest_trigger is None
                            or int(event.get("seq", 0) or 0) > int(latest_trigger.get("seq", 0) or 0)
                        ):
                            latest_trigger = event
                latest_trigger_seq = int((latest_trigger or {}).get("seq", 0) or 0)
                consumed_seq = int((consumed.get(uid) or {}).get(idx_i, 0) or 0)

                status = "ready"
                status_detail = "Ready"
                remaining_s = 0.0
                if active_action == (uid, idx_i):
                    status = "running"
                    status_detail = "Running"
                elif not bool(cfg.get("enabled", False)):
                    status = "disabled"
                    status_detail = "Engine disabled"
                elif not bool(rule.enabled):
                    status = "disabled"
                    status_detail = "Row disabled"
                elif pid <= 0:
                    status = "blocked"
                    status_detail = "No Roblox window"
                elif in_menu is True:
                    status = "blocked"
                    status_detail = "In main menu"
                elif in_menu is None:
                    status = "blocked"
                    status_detail = "Menu state unknown"
                elif (
                    now - float(not_in_menu_since.get(self._menu_gate_key(uid, pid), now))
                ) < float(rule.startup_delay_s):
                    status = "waiting"
                    remaining_s = max(
                        0.0,
                        float(rule.startup_delay_s)
                        - (now - float(not_in_menu_since.get(self._menu_gate_key(uid, pid), now))),
                    )
                    status_detail = "Startup delay"
                elif use_at > 0.0:
                    if send_at is not None:
                        status = "alert"
                        remaining_s = max(0.0, send_at - now)
                        status_detail = "Alert queued" if send_at > now else "Sending alert"
                    else:
                        status = "waiting"
                        remaining_s = max(0.0, use_at - now)
                        status_detail = "Action queued"
                elif ready_at > now:
                    status = "cooldown"
                    remaining_s = ready_at - now
                    status_detail = "Cooldown"
                elif not self._eligible_in_biome(biome, rule):
                    status = "blocked"
                    status_detail = "Biome blocked"
                elif rule.repeat_mode in ("count", "once_per_pid") and is_completed:
                    status = "complete"
                    status_detail = "Startup play count complete" if rule.repeat_mode == "count" else "Complete for PID"
                elif rule.trigger_type == "ocr_filter" and rule.trigger_filter_ids:
                    if latest_trigger_seq > consumed_seq:
                        status = "triggered"
                        status_detail = "OCR triggered"
                    else:
                        status = "waiting"
                        status_detail = "Waiting for OCR"
                elif rule.trigger_type == "merchant" and rule.trigger_merchants:
                    if latest_trigger_seq > consumed_seq:
                        status = "triggered"
                        status_detail = "Merchant triggered"
                    else:
                        status = "waiting"
                        status_detail = "Waiting for merchant"
                elif rule.trigger_type == "action_row":
                    status = "waiting"
                    status_detail = "Waiting for action row call"

                rows.append(
                    {
                        "uid": uid,
                        "username": username,
                        "action_index": idx_i,
                        "action_row_id": str(rule.row_id),
                        "action_name": str(rule.name),
                        "action_enabled": bool(rule.enabled),
                        "status": status,
                        "status_detail": status_detail,
                        "remaining_s": float(remaining_s),
                        "cooldown_s": float(rule.cooldown_s),
                        "startup_delay_s": float(rule.startup_delay_s),
                        "ready_at": ready_at,
                        "pending_use_at": use_at,
                        "pending_send_at": send_at,
                        "pid": pid,
                        "biome": biome,
                        "in_menu": in_menu,
                        "trigger_type": str(rule.trigger_type),
                        "trigger_filter_ids": list(rule.trigger_filter_ids),
                        "trigger_merchants": list(rule.trigger_merchants),
                        "last_trigger_at": float((latest_trigger or {}).get("ts", 0.0) or 0.0),
                        "repeat_mode": str(rule.repeat_mode),
                        "repeat_count": int(rule.repeat_count),
                        "allowed_biomes": list(rule.allowed_biomes),
                    }
                )

        return {
            "timestamp": now,
            "engine_running": self.is_running(),
            "enabled": bool(cfg.get("enabled", False)),
            "selected_user_count": len(users),
            "row_count": len(rows),
            "active_action": active_action,
            "rows": rows,
        }

    def record_ocr_filter_trigger(self, uid: str, pid: int, filter_id: str, filter_name: str = "") -> None:
        uid_s = str(uid or "").strip()
        filter_id_s = str(filter_id or "").strip()
        if not uid_s or not filter_id_s:
            return
        with self._state_lock:
            self._trigger_seq += 1
            self._trigger_events.setdefault(uid_s, {})[filter_id_s] = {
                "seq": int(self._trigger_seq),
                "ts": float(time.time()),
                "pid": int(pid or 0),
                "name": str(filter_name or filter_id_s),
            }

    def record_merchant_trigger(self, uid: str, pid: int, merchant: str) -> None:
        uid_s = str(uid or "").strip()
        merchants = _normalize_merchants([merchant])
        if not uid_s or not merchants:
            return
        merchant_s = merchants[0]
        with self._state_lock:
            self._trigger_seq += 1
            self._merchant_events.setdefault(uid_s, {})[merchant_s] = {
                "seq": int(self._trigger_seq),
                "ts": float(time.time()),
                "pid": int(pid or 0),
                "name": merchant_s,
            }

    def _play_action_row_now(
        self,
        hwnd: int,
        uid: str,
        pid: int,
        target_row_id: str,
        *,
        click_delay: float,
        target_row_name: str = "",
        source_row_id: str = "",
        source_row_name: str = "",
        chain: Sequence[str] = (),
    ) -> bool:
        """Synchronously play a referenced action row, then return to its caller."""
        uid_s = str(uid or "").strip()
        target_id = str(target_row_id or "").strip()
        target_label = str(target_row_name or target_id or "unknown row").strip()

        def _skip(reason: str) -> bool:
            try:
                self._log(f"[Auto-Actions] {uid_s or 'unknown user'}: action row '{target_label}' skipped ({reason}).")
            except Exception:
                pass
            return False

        if not uid_s or not target_id:
            return _skip("missing target")

        cfg = self._cfg_snapshot()
        if not bool(cfg.get("enabled", False)):
            return _skip("engine disabled")
        if uid_s not in self._users_from_cfg(cfg):
            return _skip("user is not enabled for Auto Actions")

        raw_items = cfg.get("items") or []
        raw_target: Optional[Dict[str, Any]] = None
        target_index = -1
        if isinstance(raw_items, list):
            for idx, raw in enumerate(raw_items):
                if not isinstance(raw, dict):
                    continue
                row_id = str(raw.get("id") or raw.get("row_id") or f"row_{idx + 1}").strip() or f"row_{idx + 1}"
                if row_id == target_id:
                    raw_target = raw
                    target_index = int(idx)
                    break
        if raw_target is None:
            return _skip("target row no longer exists")
        if not bool(raw_target.get("enabled", True)):
            return _skip("target row is disabled")

        target_rule: Optional[ActionRule] = None
        for idx, rule in self._rules_from_cfg(cfg, uid=uid_s):
            if int(idx) == target_index and str(rule.row_id) == target_id:
                target_rule = rule
                break
        if target_rule is None:
            return _skip("user is not enabled for the target row")
        if target_rule.trigger_type != "action_row":
            return _skip("target row does not use the Play Action Row trigger")
        if not self._menu_gate_allows(uid_s, int(pid or 0), float(target_rule.startup_delay_s)):
            return _skip("startup delay or menu gate is active")

        try:
            biome = str(self._biome_provider(uid_s) or "").strip()
        except Exception:
            biome = ""
        if not self._eligible_in_biome(biome, target_rule):
            return _skip("current biome is not allowed")

        source_id = str(source_row_id or "").strip()
        event_chain = tuple(str(value or "").strip() for value in (chain or ()) if str(value or "").strip())
        if not event_chain and source_id:
            event_chain = (source_id,)
        if target_id in event_chain:
            return _skip("action-row trigger cycle detected")
        event_chain = event_chain + (target_id,)

        with self._state_lock:
            if time.time() < float((self._next_ready.get(uid_s) or {}).get(int(target_index), 0.0) or 0.0):
                return _skip("target row is on cooldown")
            if (
                target_rule.repeat_mode == "count"
                and int(target_index) in (self._completed_startup_rows.get(uid_s) or set())
            ):
                return _skip("playback is already complete for this manager startup")
            if (
                target_rule.repeat_mode == "once_per_pid"
                and int(pid or 0)
                in ((self._completed_pids.get(uid_s) or {}).get(int(target_index), set()) or set())
            ):
                return _skip("playback is already complete for this PID")
            previous_active = self._active_action
            previous_row_id = str(self._active_action_row_id or "")
            previous_chain = tuple(self._active_action_chain or ())
            self._active_action = (uid_s, int(target_index))
            self._active_action_row_id = target_id
            self._active_action_chain = event_chain

        try:
            if self._rule_pre_alert_enabled(target_rule):
                reason = self._alert_antiafk_delay_reason(target_rule)
                if reason:
                    return _skip(reason)
                lead_s = max(0.0, float(target_rule.alert_lead_s or 0.0))
                use_at = time.time() + lead_s
                if self._schedule_alert(uid_s, target_rule, use_at=use_at) and lead_s > 0.0:
                    self._stop.wait(timeout=lead_s)
                    if self._stop.is_set():
                        return _skip("engine is stopping")

            if bool(cfg.get("menu_debug", False)):
                try:
                    biome_for_log = str(self._biome_provider(uid_s) or "").strip()
                except Exception:
                    biome_for_log = ""
                self._log_menu_activation_debug(
                    uid=uid_s,
                    pid=int(pid or 0),
                    hwnd=int(hwnd or 0),
                    biome=biome_for_log,
                    row_index=int(target_index),
                    rule=target_rule,
                    min_not_in_menu_s=float(target_rule.startup_delay_s),
                    phase="nested-activate",
                )

            self._run_rule_on_window(
                int(hwnd),
                target_rule,
                uid=uid_s,
                pid=int(pid or 0),
                click_delay=float(click_delay),
                times=self._execution_count(target_rule),
                block_user_mouse_move=bool(cfg.get("disable_mouse_move", False)),
            )
            self._mark_used(uid_s, int(pid or 0), [(int(target_index), target_rule)])
            try:
                self._log(
                    f"[Auto-Actions] {uid_s}: played nested action row '{target_rule.name}'"
                    + (f" from '{source_row_name}'" if str(source_row_name or "").strip() else "")
                    + "."
                )
            except Exception:
                pass
            return True
        finally:
            with self._state_lock:
                self._active_action = previous_active
                self._active_action_row_id = previous_row_id
                self._active_action_chain = previous_chain

    def _cfg_snapshot(self) -> Dict[str, Any]:
        with self._cfg_lock:
            return copy.deepcopy(self._cfg or {})

    def _enabled_snapshot(self) -> bool:
        """Read the stop switch without copying the complete action document."""
        with self._cfg_lock:
            return bool((self._cfg or {}).get("enabled", False))

    def _users_from_cfg(self, cfg: Dict[str, Any]) -> List[str]:
        selected: List[str] = []
        selected_set: set[str] = set()
        for raw_uid in (cfg.get("users") or []):
            uid = str(raw_uid).strip()
            if uid and uid not in selected_set:
                selected.append(uid)
                selected_set.add(uid)

        if _normalize_user_filter_mode(cfg.get("user_filter_mode", "whitelist")) != "blacklist":
            return selected

        provider = self._user_ids_provider
        if provider is None:
            return []
        try:
            available = provider() or []
        except Exception:
            return []

        resolved: List[str] = []
        seen: set[str] = set()
        for raw_uid in available:
            uid = str(raw_uid).strip()
            if not uid or uid in selected_set or uid in seen:
                continue
            seen.add(uid)
            resolved.append(uid)
        return resolved

    def _username(self, uid: str) -> str:
        fn = self._username_provider
        if fn is None:
            return str(uid)
        try:
            return str(fn(str(uid)) or "").strip() or str(uid)
        except Exception:
            return str(uid)

    def _server_label(self, uid: str) -> str:
        fn = self._server_label_provider
        if fn is None:
            return ""
        try:
            return str(fn(str(uid)) or "").strip()
        except Exception:
            return ""

    def _ps_link(self, uid: str) -> str:
        fn = self._ps_link_provider
        if fn is None:
            return ""
        try:
            return str(fn(str(uid)) or "").strip()
        except Exception:
            return ""

    def _discord_ping(self, uid: str) -> str:
        fn = self._discord_ping_provider
        if fn is None:
            return ""
        try:
            return str(fn(str(uid)) or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _item_applies_to_uid(raw: object, uid: str) -> bool:
        if not isinstance(raw, dict):
            return False
        uid_s = str(uid).strip()
        selected_users = _normalize_user_id_list(raw.get("users", None))
        if not isinstance(selected_users, list):
            return True

        user_filter_mode = _normalize_user_filter_mode(raw.get("user_filter_mode", "whitelist"))
        if user_filter_mode == "blacklist":
            return not selected_users or uid_s not in selected_users
        if selected_users:
            return uid_s in selected_users
        return not bool(raw.get("users_explicit", False))

    @staticmethod
    def _compile_item_user_filters(cfg: Dict[str, Any]) -> Dict[int, Tuple[Optional[frozenset[str]], str, bool]]:
        compiled: Dict[int, Tuple[Optional[frozenset[str]], str, bool]] = {}
        raw_items = cfg.get("items") or []
        if not isinstance(raw_items, list):
            return compiled
        for idx, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue
            selected = _normalize_user_id_list(raw.get("users", None))
            compiled[idx] = (
                None if selected is None else frozenset(str(uid) for uid in selected),
                _normalize_user_filter_mode(raw.get("user_filter_mode", "whitelist")),
                bool(raw.get("users_explicit", False)),
            )
        return compiled

    @staticmethod
    def _compiled_item_applies_to_uid(
        spec: Optional[Tuple[Optional[frozenset[str]], str, bool]],
        uid: str,
    ) -> bool:
        if spec is None:
            return False
        selected, mode, users_explicit = spec
        if selected is None:
            return True
        uid_s = str(uid).strip()
        if mode == "blacklist":
            return not selected or uid_s not in selected
        if selected:
            return uid_s in selected
        return not users_explicit

    def _parsed_rules_for_uid(
        self,
        rules: Sequence[Tuple[int, ActionRule]],
        user_filters: Dict[int, Tuple[Optional[frozenset[str]], str, bool]],
        uid: str,
    ) -> List[Tuple[int, ActionRule]]:
        return [
            (idx, rule)
            for idx, rule in rules
            if self._compiled_item_applies_to_uid(user_filters.get(int(idx)), uid)
        ]

    def _rules_from_cfg(self, cfg: Dict[str, Any], *, uid: Optional[str] = None) -> List[Tuple[int, ActionRule]]:
        out: List[Tuple[int, ActionRule]] = []
        raw_items = cfg.get("items") or []
        if not isinstance(raw_items, list):
            raw_items = []

        uid_s = str(uid).strip() if uid is not None else None

        for idx, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue

            if uid_s is not None and not self._item_applies_to_uid(raw, uid_s):
                continue

            actions_raw = raw.get("actions") or []
            if not isinstance(actions_raw, list):
                actions_raw = []
            actions: List[ActionStep] = []
            for action_idx, action_raw in enumerate(actions_raw):
                step = _normalize_action_step(action_raw, fallback_name=f"Action {action_idx + 1}")
                if step is not None:
                    actions.append(step)
            if not actions:
                continue

            behavior = _normalize_behavior(raw.get("behavior"), legacy=raw)
            name = str(raw.get("name") or f"Action {idx + 1}").strip() or f"Action {idx + 1}"

            try:
                alert_lead_s = max(0.0, float(raw.get("alert_lead_s", 15.0) or 15.0))
            except Exception:
                alert_lead_s = 15.0

            out.append(
                (
                    int(idx),
                    ActionRule(
                        row_id=str(raw.get("id") or raw.get("row_id") or f"row_{idx + 1}").strip()
                        or f"row_{idx + 1}",
                        name=name,
                        actions=tuple(actions),
                        cooldown_s=float(behavior.get("cooldown", 0.0) or 0.0),
                        startup_delay_s=float(behavior.get("startup_delay", 10.0)),
                        allowed_biomes=tuple(behavior.get("biomes") or ()),
                        trigger_type=str(behavior.get("trigger_type") or "ocr_filter"),
                        trigger_filter_ids=tuple(behavior.get("filter_ids") or ()),
                        trigger_merchants=tuple(behavior.get("merchants") or ()),
                        repeat_mode=str(behavior.get("repeat_mode") or "repeat"),
                        repeat_count=max(1, int(behavior.get("repeat_count", 1) or 1)),
                        enabled=bool(raw.get("enabled", True)),
                        alert_enabled=bool(raw.get("alert_enabled", False)),
                        alert_lead_s=alert_lead_s,
                        alert_webhook=str(raw.get("alert_webhook") or raw.get("alert_webhook_url") or "").strip(),
                        alert_message=str(raw.get("alert_message") or ""),
                    ),
                )
            )

        return out

    @staticmethod
    def _eligible_in_biome(biome: str, rule: ActionRule) -> bool:
        allowed = tuple(str(b).strip().upper() for b in (rule.allowed_biomes or ()) if str(b).strip())
        if not allowed:
            return True
        biome_s = str(biome or "").strip().upper()
        if not biome_s:
            return False
        return biome_s in allowed

    @staticmethod
    def _menu_gate_key(uid: str, pid: int) -> Tuple[str, int]:
        return (str(uid or "").strip(), int(pid or 0))

    def _prune_menu_gate_for_uid_locked(self, uid: str, pid: int) -> None:
        uid_s = str(uid or "").strip()
        pid_i = int(pid or 0)
        for key in list(self._not_in_menu_since.keys()):
            try:
                key_uid, key_pid = key
            except Exception:
                self._not_in_menu_since.pop(key, None)
                continue
            if str(key_uid) == uid_s and int(key_pid or 0) != pid_i:
                self._not_in_menu_since.pop(key, None)

    def _menu_gate_allows(self, uid: str, pid: int, min_not_in_menu_s: float) -> bool:
        if self._in_menu_provider is None:
            return False
        uid_s, pid_i = self._menu_gate_key(uid, pid)
        if not uid_s or pid_i <= 0:
            return False

        try:
            in_menu = self._in_menu_provider(uid_s)
        except Exception:
            in_menu = None

        now = time.time()
        with self._state_lock:
            self._prune_menu_gate_for_uid_locked(uid_s, pid_i)
            key = self._menu_gate_key(uid_s, pid_i)
            if in_menu is None or bool(in_menu):
                self._not_in_menu_since.pop(key, None)
                return False

            if float(min_not_in_menu_s) <= 0.0:
                self._not_in_menu_since.setdefault(key, now)
                return True

            started = self._not_in_menu_since.get(key)
            if started is None:
                self._not_in_menu_since[key] = now
                return False
            return (now - float(started)) >= float(min_not_in_menu_s)

    def _currently_not_in_menu(self, uid: str, pid: Optional[int] = None) -> bool:
        if self._in_menu_provider is None:
            return False
        try:
            in_menu = self._in_menu_provider(str(uid))
        except Exception:
            in_menu = None
        if in_menu is None:
            return False
        return not bool(in_menu)

    def _raise_if_menu_blocked(self, uid: str, pid: Optional[int] = None) -> None:
        uid_s = str(uid or "").strip()
        if not uid_s:
            return
        if not self._currently_not_in_menu(uid_s, pid):
            with self._state_lock:
                if pid is None:
                    for key in list(self._not_in_menu_since.keys()):
                        try:
                            key_uid = key[0]
                        except Exception:
                            key_uid = ""
                        if str(key_uid) == uid_s:
                            self._not_in_menu_since.pop(key, None)
                else:
                    pid_i = int(pid or 0)
                    self._prune_menu_gate_for_uid_locked(uid_s, pid_i)
                    self._not_in_menu_since.pop(self._menu_gate_key(uid_s, pid_i), None)
            raise _MenuGateBlocked("user appears to be in the main menu (or status unknown)")

    def _menu_gate_elapsed_s(self, uid: str, pid: int) -> Optional[float]:
        try:
            key = self._menu_gate_key(uid, int(pid or 0))
        except Exception:
            return None
        with self._state_lock:
            started = self._not_in_menu_since.get(key)
        if started is None:
            return None
        try:
            return max(0.0, time.time() - float(started))
        except Exception:
            return None

    def _read_in_menu_state(self, uid: str) -> Optional[bool]:
        if self._in_menu_provider is None:
            return None
        try:
            val = self._in_menu_provider(str(uid))
        except Exception:
            return None
        return None if val is None else bool(val)

    def _read_log_filename(self, uid: str) -> str:
        if self._log_filename_provider is None:
            return ""
        try:
            return str(self._log_filename_provider(str(uid)) or "").strip()
        except Exception:
            return ""

    def _log_menu_activation_debug(
        self,
        *,
        uid: str,
        pid: int,
        hwnd: int,
        biome: str,
        row_index: int,
        rule: ActionRule,
        min_not_in_menu_s: float,
        phase: str = "activate",
    ) -> None:
        in_menu = self._read_in_menu_state(uid)
        if in_menu is None:
            menu_s = "unknown"
        else:
            menu_s = "True" if bool(in_menu) else "False"
        elapsed = self._menu_gate_elapsed_s(uid, pid)
        if elapsed is None:
            gate_s = f"unset/{float(min_not_in_menu_s):.1f}s"
        else:
            gate_s = f"{float(elapsed):.2f}s/{float(min_not_in_menu_s):.1f}s"
        log_file = self._read_log_filename(uid) or "unknown"
        self._log(
            f"[Auto-Actions Menu-Debug] {phase}: uid={uid} pid={int(pid or 0)} "
            f"hwnd={int(hwnd or 0)} row={int(row_index) + 1} rule='{rule.name}' "
            f"in_menu={menu_s} gate={gate_s} log_file='{log_file}'"
            + (f" biome={biome}" if biome else "")
        )

    def _latest_trigger_event(self, uid: str, rule: ActionRule) -> Optional[Dict[str, Any]]:
        if rule.trigger_type == "ocr_filter" and rule.trigger_filter_ids:
            keys = rule.trigger_filter_ids
            events = self._trigger_events.get(str(uid), {})
        elif rule.trigger_type == "merchant" and rule.trigger_merchants:
            keys = rule.trigger_merchants
            events = self._merchant_events.get(str(uid), {})
        else:
            return None
        latest: Optional[Dict[str, Any]] = None
        for key in keys:
            event = events.get(str(key))
            if not event:
                continue
            if latest is None or int(event.get("seq", 0) or 0) > int(latest.get("seq", 0) or 0):
                latest = event
        return latest

    def _rule_pre_alert_enabled(self, rule: ActionRule) -> bool:
        try:
            return bool(
                self._alert_service.pre_sequence_alert_enabled(
                    enabled=bool(rule.alert_enabled),
                    webhook_url=str(rule.alert_webhook or ""),
                    lead_s=float(rule.alert_lead_s),
                )
            )
        except Exception:
            return False

    def _alert_antiafk_delay_reason(self, rule: ActionRule) -> str:
        try:
            return str(
                self._alert_service.antiafk_delay_reason(
                    self._antiafk_overdue_within_provider,
                    float(rule.alert_lead_s),
                )
                or ""
            ).strip()
        except Exception:
            return ""

    def _alert_antiafk_overdue_reason(self) -> str:
        try:
            return str(
                self._alert_service.antiafk_overdue_reason(self._antiafk_overdue_within_provider) or ""
            ).strip()
        except Exception:
            return ""

    def _antiafk_is_overdue_within(self, within_s: float) -> bool:
        provider = self._antiafk_overdue_within_provider
        if provider is None:
            return False
        try:
            return bool(provider(max(0.0, float(within_s or 0.0))))
        except Exception:
            return False

    def _antiafk_execution_guard_s(
        self,
        due: Sequence[Tuple[int, ActionRule]],
        *,
        click_delay: float,
    ) -> float:
        estimated_s = 0.0
        for _idx, rule in due:
            try:
                once_s = self._estimate_steps_duration(rule.actions, click_delay=click_delay)
                estimated_s += once_s * self._execution_count(rule)
                estimated_s += max(0.05, float(click_delay or 0.0))
            except Exception:
                estimated_s += _ANTIAFK_EXECUTION_GUARD_FLOOR_S
        return max(_ANTIAFK_EXECUTION_GUARD_FLOOR_S, estimated_s + 2.0)

    def _log_alert_delay(self, reason: str) -> None:
        reason_s = str(reason or "").strip()
        if not reason_s:
            return
        try:
            now_ts = time.time()
            if (now_ts - float(self._last_antiafk_alert_log_ts)) < 30.0:
                return
            self._last_antiafk_alert_log_ts = float(now_ts)
            self._log(f"[Auto-Actions] {reason_s}; delaying pre-sequence alerts until timing is safe.")
        except Exception:
            pass

    def _schedule_alert(self, uid: str, rule: ActionRule, *, use_at: float) -> bool:
        if _AutoActionAlertRequest is None:
            return False
        try:
            request = _AutoActionAlertRequest(
                webhook_url=str(rule.alert_webhook or "").strip(),
                message=str(rule.alert_message or ""),
                action_name=str(rule.name or ""),
                username=self._username(uid),
                server_label=self._server_label(uid),
                ps_link=self._ps_link(uid),
                use_at_epoch=float(use_at),
            )
        except Exception:
            return False

        try:
            sent = bool(self._alert_service.send_pre_sequence_alert(request))
        except Exception:
            sent = False
        if not sent:
            return False

        try:
            delay_s = max(0.0, float(use_at) - time.time())
            self._log(f"[Auto-Actions] {uid}: alert scheduled for '{rule.name}' in {delay_s:.1f}s")
        except Exception:
            pass
        return True

    def _mark_used(self, uid: str, pid: int, used: Sequence[Tuple[int, ActionRule]]) -> None:
        now = time.time()
        with self._state_lock:
            next_ready = self._next_ready.setdefault(str(uid), {})
            completed_pids = self._completed_pids.setdefault(str(uid), {})
            completed_startup_rows = self._completed_startup_rows.setdefault(str(uid), set())
            pending = self._pending_use_at.setdefault(str(uid), {})
            pending_send = self._pending_alert_send_at.setdefault(str(uid), {})
            pending_seq = self._pending_trigger_seq.setdefault(str(uid), {})
            for idx, rule in used:
                next_ready[int(idx)] = now + max(0.0, float(rule.cooldown_s))
                pending.pop(int(idx), None)
                pending_send.pop(int(idx), None)
                pending_seq.pop(int(idx), None)
                if str(rule.repeat_mode) == "count":
                    completed_startup_rows.add(int(idx))
                elif str(rule.repeat_mode) == "once_per_pid":
                    completed_pids.setdefault(int(idx), set()).add(int(pid or 0))

    def _condition_matches(self, hwnd: int, condition: ActionCondition, *, click_delay: float) -> bool:
        if not bool(condition.enabled):
            return True

        if condition.kind == "ocr":
            provider = self._ocr_text_provider
            if provider is None:
                try:
                    self._log("[Auto-Actions] OCR conditional skipped; OCR reader is not available.")
                except Exception:
                    pass
                time.sleep(max(0.01, float(click_delay)))
                return False
            try:
                text = str(provider(int(hwnd), _condition_payload(condition)) or "")
            except Exception as e:
                try:
                    self._log(f"[Auto-Actions] OCR conditional failed: {e}")
                except Exception:
                    pass
                time.sleep(max(0.01, float(click_delay)))
                return False
            return _ocr_text_matches(text, condition)

        color_checks = tuple(condition.color_checks or ())
        if not color_checks and condition.point is not None:
            color_checks = (
                ColorConditionCheck(
                    point=condition.point,
                    color_hex=condition.color_hex,
                    tolerance=condition.tolerance,
                ),
            )
        if not color_checks:
            time.sleep(max(0.01, float(click_delay)))
            return False

        for check in color_checks:
            abs_xy = _abs_from_rel(hwnd, check.point)
            if not abs_xy:
                time.sleep(max(0.01, float(click_delay)))
                return False

            expected = _hex_to_rgb(check.color_hex)
            tolerance = int(check.tolerance or 0)
            sampled = [
                px
                for px in (
                    _window_rel_pixel_rgb(hwnd, check.point),
                    _screen_pixel_rgb(*abs_xy),
                )
                if px is not None
            ]
            if not sampled:
                time.sleep(max(0.01, float(click_delay)))
                return False
            if not any(_color_close(px, expected, tolerance) for px in sampled):
                return False
        return True

    @staticmethod
    def _find_if_bounds(actions: Sequence[ActionStep], start_idx: int, end_idx: Optional[int] = None) -> Tuple[Optional[int], int]:
        limit = len(actions) if end_idx is None else max(0, min(len(actions), int(end_idx)))
        depth = 0
        for idx in range(int(start_idx) + 1, limit):
            kind = str(actions[idx].kind or "")
            if kind in ("if", "loop"):
                depth += 1
            elif kind in ("end", "endif", "end_if", "end_loop", "end_block"):
                if depth == 0:
                    return None, idx
                depth -= 1
            elif kind == "else" and depth == 0:
                else_idx = idx
                for end_scan in range(idx + 1, limit):
                    scan_kind = str(actions[end_scan].kind or "")
                    if scan_kind in ("if", "loop"):
                        depth += 1
                    elif scan_kind in ("end", "endif", "end_if", "end_loop", "end_block"):
                        if depth == 0:
                            return else_idx, end_scan
                        depth -= 1
                return else_idx, limit
        return None, limit

    @staticmethod
    def _find_block_end_index(actions: Sequence[ActionStep], start_idx: int, end_idx: Optional[int] = None) -> int:
        limit = len(actions) if end_idx is None else max(0, min(len(actions), int(end_idx)))
        depth = 0
        for idx in range(int(start_idx) + 1, limit):
            kind = str(actions[idx].kind or "")
            if kind in ("if", "loop"):
                depth += 1
            elif kind in ("end", "endif", "end_if", "end_loop", "end_block"):
                if depth == 0:
                    return idx
                depth -= 1
        return limit

    @staticmethod
    def _paste_text_for_user(step: ActionStep, uid: str) -> str:
        if bool(step.paste_per_user):
            uid_s = str(uid or "").strip()
            if uid_s:
                for value_uid, value_text in tuple(step.paste_user_values or ()):
                    if str(value_uid) == uid_s:
                        return str(value_text if value_text is not None else "")
        return str(step.text or "")

    def _webhook_replacements_for_step(self, step: ActionStep, uid: str, rule_name: str) -> Dict[str, str]:
        uid_s = str(uid or "").strip()
        username = self._username(uid_s) if uid_s else ""
        server_label = self._server_label(uid_s) if uid_s else ""
        ps_link = self._ps_link(uid_s) if uid_s else ""
        discord_ping = self._discord_ping(uid_s) if uid_s else ""
        try:
            biome = str(self._biome_provider(uid_s) or "").strip() if uid_s else ""
        except Exception:
            biome = ""
        now = _dt.datetime.now().astimezone()
        return {
            "user_id": uid_s,
            "uid": uid_s,
            "username": username or uid_s or "Unknown",
            "server": server_label,
            "server_label": server_label,
            "private_server": ps_link or server_label,
            "ps_link": ps_link,
            "discord_ping": discord_ping,
            "biome": biome,
            "action": str(rule_name or "").strip(),
            "row": str(rule_name or "").strip(),
            "step": str(step.name or "").strip(),
            "time": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "timestamp": now.isoformat(timespec="seconds"),
        }

    @staticmethod
    def _render_webhook_template(template: str, replacements: Dict[str, str], *, limit: int) -> str:
        message = str(template or "")
        for key, value in replacements.items():
            message = message.replace("{" + key + "}", str(value or ""))
        return message[: max(0, int(limit))].strip()

    def _webhook_message_for_step(self, step: ActionStep, uid: str, rule_name: str) -> str:
        template = str(step.webhook_message or "").strip()
        if not template:
            template = "Auto-Actions webhook step reached for {username}."
        replacements = self._webhook_replacements_for_step(step, uid, rule_name)
        message = self._render_webhook_template(template, replacements, limit=2000)
        return message or "Auto-Actions webhook step reached."

    def _webhook_embed_for_step(self, step: ActionStep, uid: str, rule_name: str) -> Tuple[str, str]:
        if not bool(step.webhook_embed_enabled):
            return "", ""
        replacements = self._webhook_replacements_for_step(step, uid, rule_name)
        return (
            self._render_webhook_template(step.webhook_embed_title, replacements, limit=256),
            self._render_webhook_template(step.webhook_embed_description, replacements, limit=4096),
        )

    def _webhook_screenshot_for_step(self, step: ActionStep, hwnd: int) -> Optional[bytes]:
        if not bool(step.webhook_include_screenshot) or step.webhook_screenshot_roi is None:
            return None
        if _capture_window_image is None or not hwnd:
            return None
        roi = step.webhook_screenshot_roi
        try:
            image = _capture_window_image(
                int(hwnd),
                (float(roi.x), float(roi.y), float(roi.w), float(roi.h)),
            )
            if image is None:
                return None
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
        except Exception:
            return None

    def _send_webhook_step(self, step: ActionStep, uid: str, rule_name: str, hwnd: int = 0) -> None:
        url = str(step.webhook_url or "").strip()
        if not url:
            try:
                self._log(f"[Auto-Actions] {uid}: webhook step '{step.name}' skipped (missing URL).")
            except Exception:
                pass
            return

        message = self._webhook_message_for_step(step, uid, rule_name)
        embed_title, embed_description = self._webhook_embed_for_step(step, uid, rule_name)
        screenshot = self._webhook_screenshot_for_step(step, hwnd)
        if bool(step.webhook_include_screenshot) and screenshot is None:
            try:
                self._log(
                    f"[Auto-Actions] {uid}: webhook step '{step.name}' could not capture its screenshot area; "
                    "sending without it."
                )
            except Exception:
                pass
        try:
            sent = bool(
                self._alert_service.send_step_webhook(
                    webhook_url=url,
                    message=message,
                    embed_title=embed_title,
                    embed_description=embed_description,
                    screenshot=screenshot,
                )
            )
        except Exception:
            sent = False
        if sent:
            try:
                self._log(f"[Auto-Actions] {uid}: webhook step '{step.name}' sent.")
            except Exception:
                pass
        else:
            try:
                self._log(f"[Auto-Actions] {uid}: webhook step '{step.name}' skipped (webhook support unavailable).")
            except Exception:
                pass

    def _kill_user_step(self, step: ActionStep, uid: str) -> None:
        uid_s = str(uid or "").strip()
        if not uid_s:
            try:
                self._log(f"[Auto-Actions] kill step '{step.name}' skipped (missing user id).")
            except Exception:
                pass
            return

        callback = self._kill_user_callback
        if callback is None:
            try:
                self._log(f"[Auto-Actions] {uid_s}: kill step '{step.name}' skipped (kill callback unavailable).")
            except Exception:
                pass
            return

        try:
            queued = bool(callback(uid_s))
        except Exception as e:
            try:
                self._log(f"[Auto-Actions] {uid_s}: kill step '{step.name}' failed: {e!r}")
            except Exception:
                pass
            return

        try:
            if queued:
                self._log(f"[Auto-Actions] {uid_s}: kill step '{step.name}' queued process termination.")
            else:
                self._log(f"[Auto-Actions] {uid_s}: kill step '{step.name}' could not queue process termination.")
        except Exception:
            pass

    def _run_action_step(
        self,
        hwnd: int,
        step: ActionStep,
        *,
        click_delay: float,
        uid: str = "",
        pid: Optional[int] = None,
        rule_name: str = "",
    ) -> bool:
        if not self._condition_matches(hwnd, step.condition, click_delay=click_delay):
            time.sleep(max(0.01, float(click_delay)))
            return False

        if step.kind == "break":
            return True

        if step.kind == "action_row":
            with self._state_lock:
                source_row_id = str(self._active_action_row_id or "")
                source_chain = tuple(self._active_action_chain or ())
            try:
                active_pid = int(pid) if pid is not None else int((self._pid_provider(uid) if uid else 0) or 0)
            except Exception:
                active_pid = 0
            self._play_action_row_now(
                int(hwnd),
                uid,
                active_pid,
                step.target_row_id,
                click_delay=float(click_delay),
                target_row_name=step.target_row_name,
                source_row_id=source_row_id,
                source_row_name=rule_name,
                chain=source_chain,
            )
            time.sleep(max(0.03, float(click_delay)))
            return False

        if step.kind == "kill_user":
            self._kill_user_step(step, uid)
            time.sleep(max(0.03, float(click_delay)))
            return True

        if step.kind == "webhook":
            self._send_webhook_step(step, uid, rule_name, hwnd)
            time.sleep(max(0.03, float(click_delay)))
            return False

        if step.kind == "paste":
            if bool(step.select_all):
                _send_ctrl_a()
                time.sleep(0.03)
            _send_unicode_text(self._paste_text_for_user(step, uid))
            time.sleep(max(0.03, float(click_delay)))
            return False

        if step.kind == "key":
            _send_keys(step.keys or ([step.key] if step.key else []), step.key_hold_s)
            time.sleep(max(0.03, float(click_delay)))
            return False

        if step.kind == "wait":
            time.sleep(max(0.0, float(step.wait_s or 0.0)))
            time.sleep(max(0.03, float(click_delay)))
            return False

        if step.point is None:
            time.sleep(max(0.01, float(click_delay)))
            return False

        abs_xy = _abs_from_rel(hwnd, step.point)
        if not abs_xy:
            time.sleep(max(0.01, float(click_delay)))
            return False

        if step.kind == "scroll":
            direction = "up" if str(step.scroll_direction or "down").strip().lower() == "up" else "down"
            delta = 120 if direction == "up" else -120
            try:
                _mouse_move_instant(*abs_xy)
                time.sleep(0.02)
                ctypes.windll.user32.mouse_event(int(win32con.MOUSEEVENTF_WHEEL), 0, 0, int(delta), 0)
            except Exception:
                try:
                    if autoit is not None:
                        autoit.mouse_wheel(direction, 1)
                except Exception:
                    pass
            time.sleep(max(0.03, float(click_delay)))
            return False

        if step.kind == "drag":
            if step.end_point is None:
                time.sleep(max(0.01, float(click_delay)))
                return False
            end_xy = _abs_from_rel(hwnd, step.end_point)
            if not end_xy:
                time.sleep(max(0.01, float(click_delay)))
                return False
            _mouse_button_drag(
                hwnd,
                int(abs_xy[0]),
                int(abs_xy[1]),
                int(end_xy[0]),
                int(end_xy[1]),
                button=str(step.click_button or "left"),
                duration_s=float(step.drag_duration_s or 0.0),
            )
            time.sleep(max(0.03, float(click_delay)))
            return False

        _mouse_button_click(
            hwnd,
            *abs_xy,
            button=str(step.click_button or "left"),
            count=int(step.click_count or 1),
        )
        time.sleep(max(0.01, float(click_delay)))
        return False

    def _run_steps_once(
        self,
        hwnd: int,
        actions: Sequence[ActionStep],
        *,
        click_delay: float,
        uid: str = "",
        pid: Optional[int] = None,
        rule_name: str = "",
    ) -> bool:
        return self._run_steps_range(
            hwnd,
            actions,
            0,
            len(actions),
            click_delay=click_delay,
            uid=uid,
            pid=pid,
            rule_name=rule_name,
        )

    def _run_steps_range(
        self,
        hwnd: int,
        actions: Sequence[ActionStep],
        start_idx: int,
        end_idx: int,
        *,
        click_delay: float,
        uid: str = "",
        pid: Optional[int] = None,
        rule_name: str = "",
    ) -> bool:
        pc = 0
        pc = max(0, int(start_idx))
        end = max(0, min(len(actions), int(end_idx)))
        while pc < end:
            self._raise_if_menu_blocked(uid, pid)
            step = actions[pc]
            kind = str(step.kind or "")
            if kind == "if":
                else_idx, block_end = self._find_if_bounds(actions, pc, end)
                if self._condition_matches(hwnd, step.condition, click_delay=click_delay):
                    true_end = else_idx if else_idx is not None else block_end
                    if self._run_steps_range(
                        hwnd,
                        actions,
                        pc + 1,
                        true_end,
                        click_delay=click_delay,
                        uid=uid,
                        pid=pid,
                        rule_name=rule_name,
                    ):
                        return True
                else:
                    if else_idx is not None:
                        if self._run_steps_range(
                            hwnd,
                            actions,
                            else_idx + 1,
                            block_end,
                            click_delay=click_delay,
                            uid=uid,
                            pid=pid,
                            rule_name=rule_name,
                        ):
                            return True
                pc = block_end + 1
                continue
            if kind == "loop":
                block_end = self._find_block_end_index(actions, pc, end)
                if self._condition_matches(hwnd, step.condition, click_delay=click_delay):
                    for _ in range(max(1, int(step.loop_count or 1))):
                        if self._run_steps_range(
                            hwnd,
                            actions,
                            pc + 1,
                            block_end,
                            click_delay=click_delay,
                            uid=uid,
                            pid=pid,
                            rule_name=rule_name,
                        ):
                            return True
                pc = block_end + 1
                continue
            if kind in ("else", "end", "endif", "end_if", "end_loop", "end_block"):
                pc += 1
                continue

            if self._run_action_step(
                hwnd,
                step,
                click_delay=click_delay,
                uid=uid,
                pid=pid,
                rule_name=rule_name,
            ):
                return True
            pc += 1
        return False

    def _run_rule_on_window(
        self,
        hwnd: int,
        rule: ActionRule,
        *,
        uid: str = "",
        pid: Optional[int] = None,
        click_delay: float,
        times: int,
        block_user_mouse_move: bool = False,
    ) -> None:
        self._raise_if_menu_blocked(uid, pid)
        with _window_topmost_during(hwnd), _block_user_mouse_movement_during_actions(
            bool(block_user_mouse_move),
            log_fn=self._log,
            notify_fn=self._mouse_block_notify,
        ), _preserve_clipboard_during_auto_action_sequence(
            any(str(step.kind or "") == "paste" for step in rule.actions)
        ):
            _bring_window_foreground(hwnd)
            _set_window_topmost(hwnd, True)
            time.sleep(max(0.02, float(click_delay) * 0.5))
            self._raise_if_menu_blocked(uid, pid)

            repeats = max(1, int(times or 1))
            for _ in range(repeats):
                self._raise_if_menu_blocked(uid, pid)
                if self._run_steps_once(
                    hwnd,
                    rule.actions,
                    click_delay=click_delay,
                    uid=uid,
                    pid=pid,
                    rule_name=rule.name,
                ):
                    break

    def _execution_count(self, rule: ActionRule) -> int:
        if rule.repeat_mode in ("count", "count_per_trigger"):
            return max(1, int(rule.repeat_count or 1))
        return 1

    def _prune_state_for_user(self, uid: str, valid_indices: set[int]) -> None:
        if not valid_indices:
            self._next_ready.pop(uid, None)
            self._pending_use_at.pop(uid, None)
            self._pending_alert_send_at.pop(uid, None)
            self._pending_trigger_seq.pop(uid, None)
            self._last_trigger_seq.pop(uid, None)
            self._completed_pids.pop(uid, None)
            self._completed_startup_rows.pop(uid, None)
            self._rule_signature.pop(uid, None)
            return

        for mapping in (
            self._next_ready.get(uid),
            self._pending_use_at.get(uid),
            self._pending_alert_send_at.get(uid),
            self._pending_trigger_seq.get(uid),
            self._last_trigger_seq.get(uid),
            self._completed_pids.get(uid),
            self._rule_signature.get(uid),
        ):
            if not isinstance(mapping, dict):
                continue
            for idx in list(mapping.keys()):
                if int(idx) not in valid_indices:
                    mapping.pop(int(idx), None)
        completed_startup_rows = self._completed_startup_rows.get(uid)
        if isinstance(completed_startup_rows, set):
            completed_startup_rows.intersection_update(valid_indices)

    @staticmethod
    def _estimate_steps_duration(actions: Sequence[ActionStep], *, click_delay: float) -> float:
        delay = max(0.01, float(click_delay or 0.0))
        total = 0.0
        pc = 0
        while pc < len(actions):
            step = actions[pc]
            kind = str(step.kind or "")
            if kind == "if":
                else_idx, block_end = AutoActionEngine._find_if_bounds(actions, pc)
                true_end = else_idx if else_idx is not None else block_end
                true_s = AutoActionEngine._estimate_steps_duration(actions[pc + 1 : true_end], click_delay=delay)
                false_s = 0.0
                if else_idx is not None:
                    false_s = AutoActionEngine._estimate_steps_duration(actions[else_idx + 1 : block_end], click_delay=delay)
                total += max(true_s, false_s, delay)
                pc = block_end + 1
                continue
            if kind == "loop":
                block_end = AutoActionEngine._find_block_end_index(actions, pc)
                inner_s = AutoActionEngine._estimate_steps_duration(actions[pc + 1 : block_end], click_delay=delay)
                total += max(1, int(step.loop_count or 1)) * max(inner_s, delay)
                pc = block_end + 1
                continue
            if kind in ("else", "end", "endif", "end_if", "end_loop", "end_block"):
                pc += 1
                continue

            if kind == "wait":
                total += max(0.0, float(step.wait_s or 0.0)) + max(0.03, delay)
            elif kind == "drag":
                total += max(0.0, float(step.drag_duration_s or 0.0)) + max(0.03, delay)
            elif kind == "key":
                total += max(0.0, float(step.key_hold_s or 0.0)) + max(0.03, delay)
            elif kind in ("paste", "webhook", "action_row", "scroll", "kill_user"):
                total += max(0.03, delay)
            else:
                total += max(0.01, delay)
            pc += 1
        return max(0.0, total)

    def _estimate_rule_slot_s(self, rule: ActionRule, *, click_delay: float) -> float:
        try:
            repeats = self._execution_count(rule)
        except Exception:
            repeats = 1
        try:
            prep_s = max(0.02, float(click_delay or 0.0) * 0.5)
        except Exception:
            prep_s = 0.1
        try:
            actions_s = self._estimate_steps_duration(tuple(rule.actions or ()), click_delay=float(click_delay or 0.0))
        except Exception:
            actions_s = max(0.2, float(click_delay or 0.2))
        return max(1.0, prep_s + max(1, int(repeats or 1)) * actions_s + 0.25)

    def _pending_alert_count_locked(self) -> int:
        total = 0
        for per_user in self._pending_use_at.values():
            if isinstance(per_user, dict):
                total += len(per_user)
        return int(total)

    def _pending_sent_alert_tail_use_at_locked(self, *, exclude_uid: str = "", exclude_idx: Optional[int] = None) -> float:
        tail = 0.0
        exclude_uid_s = str(exclude_uid or "")
        exclude_idx_i = int(exclude_idx) if exclude_idx is not None else None
        for pending_uid, per_user in self._pending_use_at.items():
            if not isinstance(per_user, dict):
                continue
            send_map = self._pending_alert_send_at.get(str(pending_uid)) or {}
            for pending_idx, value in per_user.items():
                try:
                    pending_idx_i = int(pending_idx)
                except Exception:
                    continue
                if str(pending_uid) == exclude_uid_s and exclude_idx_i is not None and pending_idx_i == exclude_idx_i:
                    continue
                if pending_idx_i in send_map:
                    continue
                try:
                    tail = max(tail, float(value))
                except Exception:
                    continue
        return float(tail)

    def _has_sent_alert_countdown_locked(self, *, exclude_uid: str = "", exclude_idx: Optional[int] = None) -> bool:
        return self._pending_sent_alert_tail_use_at_locked(exclude_uid=exclude_uid, exclude_idx=exclude_idx) > 0.0

    def _has_earlier_sent_alert_locked(self, uid: str, idx: int, use_at: float) -> bool:
        current = (float(use_at), str(uid), int(idx))
        for pending_uid, per_user in self._pending_use_at.items():
            if not isinstance(per_user, dict):
                continue
            send_map = self._pending_alert_send_at.get(str(pending_uid)) or {}
            for pending_idx, value in per_user.items():
                try:
                    pending_idx_i = int(pending_idx)
                except Exception:
                    continue
                if str(pending_uid) == str(uid) and pending_idx_i == int(idx):
                    continue
                if pending_idx_i in send_map:
                    continue
                try:
                    other = (float(value), str(pending_uid), pending_idx_i)
                except Exception:
                    continue
                if other < current:
                    return True
        return False

    def _planned_alert_times_locked(
        self,
        rule: ActionRule,
        *,
        now: float,
        click_delay: float,
    ) -> Tuple[float, float]:
        try:
            lead_s = max(0.0, float(rule.alert_lead_s or 0.0))
        except Exception:
            lead_s = 0.0

        try:
            slot_s = self._estimate_rule_slot_s(rule, click_delay=click_delay)
        except Exception:
            slot_s = 1.0
        slot_s = max(1.0, float(slot_s))

        latest_use_at = 0.0
        for per_user in self._pending_use_at.values():
            if not isinstance(per_user, dict):
                continue
            for value in per_user.values():
                try:
                    latest_use_at = max(latest_use_at, float(value))
                except Exception:
                    continue

        latest_send_at = float(getattr(self, "_last_alert_send_at", 0.0) or 0.0)
        for per_user in self._pending_alert_send_at.values():
            if not isinstance(per_user, dict):
                continue
            for value in per_user.values():
                try:
                    latest_send_at = max(latest_send_at, float(value))
                except Exception:
                    continue

        use_at = max(float(now) + lead_s, latest_use_at + slot_s)
        send_at = max(float(now), use_at - lead_s, latest_send_at + 1.0)
        use_at = max(use_at, send_at + lead_s)
        return float(send_at), float(use_at)

    def _can_reserve_alert_locked(self, *, now: float, send_at: float) -> bool:
        try:
            max_pending = max(1, int(_MAX_PENDING_PRE_SEQUENCE_ALERTS))
        except Exception:
            max_pending = 5
        if self._pending_alert_count_locked() >= max_pending:
            self._log_alert_queue_limit("queue depth")
            return False

        try:
            lookahead_s = max(1.0, float(_PRE_SEQUENCE_ALERT_LOOKAHEAD_S))
        except Exception:
            lookahead_s = 60.0
        if float(send_at) > float(now) + lookahead_s:
            self._log_alert_queue_limit("lookahead")
            return False

        return True

    def _log_alert_queue_limit(self, reason: str) -> None:
        try:
            now_ts = time.time()
            if (now_ts - float(self._last_alert_queue_limit_log_ts)) < 30.0:
                return
            self._last_alert_queue_limit_log_ts = float(now_ts)
            if str(reason or "") == "lookahead":
                self._log(
                    f"[Auto-Actions] Alert queue lookahead is full; leaving later alerts unreserved until they are within {float(_PRE_SEQUENCE_ALERT_LOOKAHEAD_S):.0f}s."
                )
            else:
                self._log(
                    f"[Auto-Actions] Alert queue is at {int(_MAX_PENDING_PRE_SEQUENCE_ALERTS)} pending reservation(s); leaving later alerts unreserved."
                )
        except Exception:
            pass

    def _reserve_alert_locked(
        self,
        uid: str,
        idx: int,
        rule: ActionRule,
        *,
        now: float,
        seq: int,
        click_delay: float,
    ) -> Tuple[float, float]:
        send_at, use_at = self._planned_alert_times_locked(rule, now=now, click_delay=click_delay)

        self._pending_use_at.setdefault(str(uid), {})[int(idx)] = float(use_at)
        self._pending_alert_send_at.setdefault(str(uid), {})[int(idx)] = float(send_at)
        if int(seq or 0) > 0:
            self._pending_trigger_seq.setdefault(str(uid), {})[int(idx)] = int(seq)
        else:
            self._pending_trigger_seq.setdefault(str(uid), {}).pop(int(idx), None)

        return float(send_at), float(use_at)

    @staticmethod
    def _alert_lead_s(rule: ActionRule) -> float:
        try:
            return max(0.0, float(rule.alert_lead_s or 0.0))
        except Exception:
            return 0.0

    def _remove_pending_alert_locked(self, uid: str, idx: int) -> None:
        uid_s = str(uid)
        idx_i = int(idx)
        pending = self._pending_use_at.get(uid_s)
        if isinstance(pending, dict):
            pending.pop(idx_i, None)
            if not pending:
                self._pending_use_at.pop(uid_s, None)
        send_map = self._pending_alert_send_at.get(uid_s)
        if isinstance(send_map, dict):
            send_map.pop(idx_i, None)
            if not send_map:
                self._pending_alert_send_at.pop(uid_s, None)
        seq_map = self._pending_trigger_seq.get(uid_s)
        if isinstance(seq_map, dict):
            seq_map.pop(idx_i, None)
            if not seq_map:
                self._pending_trigger_seq.pop(uid_s, None)

    def _dispatch_next_due_alert(
        self,
        cfg: Dict[str, Any],
        users: Sequence[str],
        *,
        click_delay: float,
        parsed_rules: Optional[Sequence[Tuple[int, ActionRule]]] = None,
        user_filters: Optional[Dict[int, Tuple[Optional[frozenset[str]], str, bool]]] = None,
    ) -> bool:
        now = time.time()
        try:
            if now < float(self._last_alert_send_at or 0.0) + 1.0:
                return False
        except Exception:
            pass

        user_order = {str(uid): pos for pos, uid in enumerate(users or [])}
        candidates: List[Tuple[float, float, int, str, int, ActionRule]] = []
        all_rules = list(parsed_rules) if parsed_rules is not None else self._rules_from_cfg(cfg)
        filters = user_filters if user_filters is not None else self._compile_item_user_filters(cfg)

        with self._state_lock:
            # Usually this is empty. Do not parse/filter rules for hundreds of
            # accounts when no pre-sequence alert is waiting.
            pending_uids = [uid for uid in self._pending_alert_send_at if str(uid) in user_order]
            for uid in pending_uids:
                uid_s = str(uid)
                rules_by_idx = {
                    int(idx): rule
                    for idx, rule in self._parsed_rules_for_uid(all_rules, filters, uid_s)
                }
                pending = self._pending_use_at.get(uid_s) or {}
                send_map = self._pending_alert_send_at.get(uid_s) or {}
                for raw_idx, raw_send_at in list(send_map.items()):
                    try:
                        idx_i = int(raw_idx)
                    except Exception:
                        continue
                    rule = rules_by_idx.get(idx_i)
                    if rule is None or idx_i not in pending:
                        self._remove_pending_alert_locked(uid_s, idx_i)
                        continue
                    if (self._rule_signature.get(uid_s) or {}).get(idx_i) != hash(rule):
                        self._remove_pending_alert_locked(uid_s, idx_i)
                        continue
                    try:
                        send_at = float(raw_send_at)
                    except Exception:
                        self._remove_pending_alert_locked(uid_s, idx_i)
                        continue
                    if now < send_at:
                        continue
                    try:
                        use_at = float(pending.get(idx_i, now))
                    except Exception:
                        use_at = now
                    candidates.append((send_at, use_at, int(user_order.get(uid_s, 999999)), uid_s, idx_i, rule))

        if not candidates:
            return False

        candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))

        for _send_at, _use_at, _order, uid, idx, rule in candidates:
            send_payload: Optional[Tuple[str, int, ActionRule, float]] = None
            with self._state_lock:
                pending = self._pending_use_at.get(str(uid)) or {}
                send_map = self._pending_alert_send_at.get(str(uid)) or {}
                if int(idx) not in pending or int(idx) not in send_map:
                    continue

                if not self._rule_pre_alert_enabled(rule):
                    self._remove_pending_alert_locked(str(uid), int(idx))
                    continue

                reason = self._alert_antiafk_delay_reason(rule)
                if reason:
                    self._log_alert_delay(reason)
                    lead_s = self._alert_lead_s(rule)
                    retry_send_at = now + 5.0
                    pending[int(idx)] = retry_send_at + lead_s
                    send_map[int(idx)] = retry_send_at
                    return False

                try:
                    current_send_at = float(send_map.get(int(idx), now))
                except Exception:
                    current_send_at = now
                try:
                    use_at = float(pending.get(int(idx), now))
                except Exception:
                    use_at = now

                lead_s = self._alert_lead_s(rule)
                slot_s = self._estimate_rule_slot_s(rule, click_delay=float(click_delay or 0.0))
                sent_tail = self._pending_sent_alert_tail_use_at_locked(exclude_uid=str(uid), exclude_idx=int(idx))
                if sent_tail > 0.0:
                    use_at = max(use_at, sent_tail + slot_s)
                if now > current_send_at:
                    use_at = max(use_at, now + lead_s)

                target_send_at = max(now, use_at - lead_s)
                if target_send_at > now + 0.25:
                    pending[int(idx)] = float(use_at)
                    send_map[int(idx)] = float(target_send_at)
                    return False

                pending[int(idx)] = float(use_at)
                send_payload = (str(uid), int(idx), rule, float(use_at))

            if send_payload is None:
                continue

            send_uid, send_idx, send_rule, send_use_at = send_payload
            sent = self._schedule_alert(send_uid, send_rule, use_at=send_use_at)
            with self._state_lock:
                pending = self._pending_use_at.get(send_uid) or {}
                if send_idx in pending and abs(float(pending.get(send_idx, 0.0)) - float(send_use_at)) < 0.5:
                    if sent:
                        send_map = self._pending_alert_send_at.setdefault(send_uid, {})
                        send_map.pop(send_idx, None)
                        if not send_map:
                            self._pending_alert_send_at.pop(send_uid, None)
                        self._last_alert_send_at = max(float(self._last_alert_send_at), time.time())
                    else:
                        self._remove_pending_alert_locked(send_uid, send_idx)
            return bool(sent)

        return False

    def _cancel_overdue_pending_alerts(self, uid: str, *, now: Optional[float] = None, reason: str = "") -> None:
        try:
            now_ts = float(now if now is not None else time.time())
        except Exception:
            now_ts = time.time()

        canceled = 0
        with self._state_lock:
            pending = self._pending_use_at.get(str(uid))
            if not pending:
                return
            pending_send = self._pending_alert_send_at.get(str(uid)) or {}
            pending_seq = self._pending_trigger_seq.get(str(uid)) or {}
            for idx, use_at in list(pending.items()):
                try:
                    if now_ts < float(use_at):
                        continue
                except Exception:
                    continue
                pending.pop(int(idx), None)
                pending_send.pop(int(idx), None)
                pending_seq.pop(int(idx), None)
                canceled += 1
            if not pending:
                self._pending_use_at.pop(str(uid), None)
            if pending_send:
                self._pending_alert_send_at[str(uid)] = pending_send
            else:
                self._pending_alert_send_at.pop(str(uid), None)
            if pending_seq:
                self._pending_trigger_seq[str(uid)] = pending_seq
            else:
                self._pending_trigger_seq.pop(str(uid), None)

        if canceled:
            try:
                why = f" ({reason})" if reason else ""
                self._log(f"[Auto-Actions] {uid}: canceled overdue alert schedule{why}")
            except Exception:
                pass

    def _evaluate_rules_for_user(
        self,
        uid: str,
        pid: int,
        biome: str,
        rules: Sequence[Tuple[int, ActionRule]],
        *,
        click_delay: float = 0.2,
    ) -> List[Tuple[int, ActionRule]]:
        now = time.time()
        due: List[Tuple[int, ActionRule]] = []

        with self._state_lock:
            valid = {int(idx) for idx, _rule in (rules or [])}
            self._prune_state_for_user(str(uid), valid)

            next_ready = self._next_ready.setdefault(str(uid), {})
            pending = self._pending_use_at.setdefault(str(uid), {})
            pending_send = self._pending_alert_send_at.setdefault(str(uid), {})
            pending_seq = self._pending_trigger_seq.setdefault(str(uid), {})
            consumed = self._last_trigger_seq.setdefault(str(uid), {})
            completed_pids = self._completed_pids.setdefault(str(uid), {})
            completed_startup_rows = self._completed_startup_rows.setdefault(str(uid), set())
            signatures = self._rule_signature.setdefault(str(uid), {})

            for idx, rule in rules:
                idx_i = int(idx)
                sig = hash(rule)
                if signatures.get(idx_i) != sig:
                    signatures[idx_i] = sig
                    next_ready.pop(idx_i, None)
                    pending.pop(idx_i, None)
                    pending_send.pop(idx_i, None)
                    pending_seq.pop(idx_i, None)
                    consumed.pop(idx_i, None)
                    completed_pids.pop(idx_i, None)
                    completed_startup_rows.discard(idx_i)

                if not rule.enabled or not rule.actions:
                    pending.pop(idx_i, None)
                    pending_send.pop(idx_i, None)
                    pending_seq.pop(idx_i, None)
                    continue

                if idx_i in pending:
                    if not self._rule_pre_alert_enabled(rule):
                        pending.pop(idx_i, None)
                        pending_send.pop(idx_i, None)
                        pending_seq.pop(idx_i, None)
                        continue

                    send_at = pending_send.get(idx_i)
                    if send_at is not None:
                        if now < float(send_at):
                            continue
                        reason = self._alert_antiafk_delay_reason(rule)
                        if reason:
                            self._log_alert_delay(reason)
                            pending.pop(idx_i, None)
                            pending_send.pop(idx_i, None)
                            seq = int(pending_seq.pop(idx_i, 0) or 0)
                            send_at, _use_at = self._planned_alert_times_locked(
                                rule,
                                now=now,
                                click_delay=float(click_delay or 0.0),
                            )
                            if not self._can_reserve_alert_locked(now=now, send_at=send_at):
                                continue
                            self._reserve_alert_locked(
                                str(uid),
                                idx_i,
                                rule,
                                now=now,
                                seq=seq,
                                click_delay=float(click_delay or 0.0),
                            )
                            continue
                        # Global alert dispatch sends due webhooks in planned order.
                        continue

                    use_at = float(pending.get(idx_i, 0.0))
                    if now < use_at:
                        continue
                    if self._has_earlier_sent_alert_locked(str(uid), idx_i, float(use_at)):
                        continue
                    pending.pop(idx_i, None)
                    seq = int(pending_seq.pop(idx_i, 0) or 0)
                    if now < float(next_ready.get(idx_i, 0.0)):
                        continue
                    if not self._eligible_in_biome(biome, rule):
                        continue
                    if rule.repeat_mode == "count" and idx_i in completed_startup_rows:
                        continue
                    if rule.repeat_mode == "once_per_pid" and int(pid or 0) in completed_pids.get(idx_i, set()):
                        continue
                    reason = self._alert_antiafk_overdue_reason()
                    if reason:
                        self._log_alert_delay(reason)
                        continue
                    if seq > 0:
                        consumed[idx_i] = seq
                    due.append((idx_i, rule))
                    continue

                alert_enabled = self._rule_pre_alert_enabled(rule)

                if self._has_sent_alert_countdown_locked():
                    continue

                is_event_trigger = bool(
                    (rule.trigger_type == "ocr_filter" and rule.trigger_filter_ids)
                    or (rule.trigger_type == "merchant" and rule.trigger_merchants)
                    or rule.trigger_type == "action_row"
                )
                if not is_event_trigger:
                    if now < float(next_ready.get(idx_i, 0.0)):
                        continue
                    if not self._eligible_in_biome(biome, rule):
                        continue
                    if rule.repeat_mode == "count" and idx_i in completed_startup_rows:
                        continue
                    if rule.repeat_mode == "once_per_pid" and int(pid or 0) in completed_pids.get(idx_i, set()):
                        continue

                    if alert_enabled:
                        reason = self._alert_antiafk_delay_reason(rule)
                        if reason:
                            self._log_alert_delay(reason)
                            continue
                        send_at, _use_at = self._planned_alert_times_locked(
                            rule,
                            now=now,
                            click_delay=float(click_delay or 0.0),
                        )
                        if not self._can_reserve_alert_locked(now=now, send_at=send_at):
                            continue
                        send_at, use_at = self._reserve_alert_locked(
                            str(uid),
                            idx_i,
                            rule,
                            now=now,
                            seq=0,
                            click_delay=float(click_delay or 0.0),
                        )
                        continue

                    due.append((idx_i, rule))
                    continue

                latest = self._latest_trigger_event(str(uid), rule)
                if latest is None:
                    continue

                seq = int(latest.get("seq", 0) or 0)
                if seq <= int(consumed.get(idx_i, 0) or 0):
                    continue

                if now < float(next_ready.get(idx_i, 0.0)):
                    continue
                if not self._eligible_in_biome(biome, rule):
                    continue
                if rule.repeat_mode == "count" and idx_i in completed_startup_rows:
                    continue
                if rule.repeat_mode == "once_per_pid" and int(pid or 0) in completed_pids.get(idx_i, set()):
                    continue

                if alert_enabled:
                    reason = self._alert_antiafk_delay_reason(rule)
                    if reason:
                        self._log_alert_delay(reason)
                        continue
                    send_at, _use_at = self._planned_alert_times_locked(
                        rule,
                        now=now,
                        click_delay=float(click_delay or 0.0),
                    )
                    if not self._can_reserve_alert_locked(now=now, send_at=send_at):
                        continue
                    send_at, use_at = self._reserve_alert_locked(
                        str(uid),
                        idx_i,
                        rule,
                        now=now,
                        seq=seq,
                        click_delay=float(click_delay or 0.0),
                    )
                    continue

                consumed[idx_i] = seq
                due.append((idx_i, rule))
        return due

    def _run(self) -> None:
        self._log("[Auto-Actions] Engine started.")
        if autoit is None:
            self._log("[Auto-Actions] AutoIt is required for input. Auto-Actions disabled.")
            return

        while not self._stop.is_set():
            cfg = self._cfg_snapshot()
            enabled = bool(cfg.get("enabled", False))
            try:
                interval = max(0.2, float(cfg.get("tick_interval", 1.0) or 1.0))
            except Exception:
                interval = 1.0

            if not enabled:
                with self._state_lock:
                    self._pending_use_at.clear()
                    self._pending_alert_send_at.clear()
                    self._pending_trigger_seq.clear()
                self._stop.wait(timeout=interval)
                continue

            users = self._users_from_cfg(cfg)
            click_delay = float(cfg.get("click_delay", 0.2) or 0.2)
            block_user_mouse_move = bool(cfg.get("disable_mouse_move", False))
            menu_debug = bool(cfg.get("menu_debug", False))
            if not users:
                with self._state_lock:
                    self._pending_use_at.clear()
                    self._pending_alert_send_at.clear()
                    self._pending_trigger_seq.clear()
                self._stop.wait(timeout=max(0.5, interval))
                continue

            # Normalize action steps and per-rule user filters once per engine
            # cycle, then reuse them for every live account.
            all_rules = self._rules_from_cfg(cfg)
            user_filters = self._compile_item_user_filters(cfg)

            try:
                active_users = {str(uid) for uid in users}
            except Exception:
                active_users = set()
            if active_users:
                with self._state_lock:
                    for pending_uid in list(self._pending_use_at.keys()):
                        if str(pending_uid) not in active_users:
                            self._pending_use_at.pop(str(pending_uid), None)
                            self._pending_alert_send_at.pop(str(pending_uid), None)
                            self._pending_trigger_seq.pop(str(pending_uid), None)

            try:
                self._dispatch_next_due_alert(
                    cfg,
                    users,
                    click_delay=click_delay,
                    parsed_rules=all_rules,
                    user_filters=user_filters,
                )
            except Exception as e:
                try:
                    self._log(f"[Auto-Actions] alert dispatch error: {e}")
                except Exception:
                    pass

            did_any = False
            paused = False
            pause_denied = False

            try:
                for uid in users:
                    if self._stop.is_set():
                        break

                    try:
                        if not self._enabled_snapshot():
                            break
                    except Exception:
                        pass

                    now_ts = time.time()

                    try:
                        pid = self._pid_provider(uid)
                    except Exception:
                        pid = None
                    if not pid:
                        self._cancel_overdue_pending_alerts(uid, now=now_ts, reason="window missing")
                        continue

                    try:
                        hwnd = self._hwnd_provider(int(pid))
                    except Exception:
                        hwnd = None
                    if not hwnd:
                        self._cancel_overdue_pending_alerts(uid, now=now_ts, reason="window missing")
                        continue

                    try:
                        biome = self._biome_provider(uid) or ""
                    except Exception:
                        biome = ""

                    if not self._menu_gate_allows(uid, int(pid), 0.0):
                        self._cancel_overdue_pending_alerts(uid, now=now_ts, reason="menu gate")
                        continue

                    rules = self._parsed_rules_for_uid(all_rules, user_filters, uid)
                    if not rules:
                        with self._state_lock:
                            self._pending_use_at.pop(str(uid), None)
                            self._pending_alert_send_at.pop(str(uid), None)
                            self._pending_trigger_seq.pop(str(uid), None)
                        continue

                    gated_rules: List[Tuple[int, ActionRule]] = []
                    for idx, rule in rules:
                        if self._menu_gate_allows(uid, int(pid), float(rule.startup_delay_s)):
                            gated_rules.append((idx, rule))
                    rules = gated_rules
                    if not rules:
                        continue

                    due = self._evaluate_rules_for_user(uid, int(pid), biome, rules, click_delay=click_delay)
                    if not due:
                        continue

                    antiafk_guard_s = self._antiafk_execution_guard_s(due, click_delay=click_delay)
                    if self._antiafk_is_overdue_within(antiafk_guard_s):
                        if self._antiafk_is_overdue_within(0.0):
                            antiafk_reason = "Anti-AFK is overdue"
                        else:
                            antiafk_reason = f"Anti-AFK is due within {antiafk_guard_s:.0f}s"
                        self._log(
                            f"[Auto-Actions] {antiafk_reason}; stopping this batch before {uid}."
                        )
                        pause_denied = True
                        break

                    if pause_denied:
                        break
                    if not paused and self._pause_antiafk:
                        try:
                            res = self._pause_antiafk()
                            if res is False:
                                pause_denied = True
                                break
                            paused = True
                        except Exception:
                            pause_denied = True
                            self._log("[Auto-Actions] Failed to pause Anti-AFK; skipping this cycle.")
                            break

                    lead_s = 0.0
                    if self._pre_action_hook is not None:
                        try:
                            lead_s = float(self._pre_action_hook(str(uid), int(pid)) or 0.0)
                        except Exception:
                            lead_s = 0.0
                    if lead_s > 0.0:
                        self._stop.wait(timeout=max(0.0, float(lead_s)))
                        if self._stop.is_set():
                            break
                    gated_due: List[Tuple[int, ActionRule]] = []
                    for idx, rule in due:
                        if self._menu_gate_allows(uid, int(pid), float(rule.startup_delay_s)):
                            gated_due.append((idx, rule))
                    due = gated_due
                    if not due:
                        continue

                    try:
                        used: List[Tuple[int, ActionRule]] = []
                        with self._action_lock:
                            for idx, rule in due:
                                with self._state_lock:
                                    self._active_action = (str(uid), int(idx))
                                    self._active_action_row_id = str(rule.row_id or "")
                                    self._active_action_chain = (
                                        (str(rule.row_id),) if str(rule.row_id or "").strip() else ()
                                    )
                                try:
                                    if menu_debug:
                                        self._log_menu_activation_debug(
                                            uid=str(uid),
                                            pid=int(pid),
                                            hwnd=int(hwnd),
                                            biome=str(biome or ""),
                                            row_index=int(idx),
                                            rule=rule,
                                            min_not_in_menu_s=float(rule.startup_delay_s),
                                            phase="activate",
                                        )
                                    self._run_rule_on_window(
                                        int(hwnd),
                                        rule,
                                        uid=str(uid),
                                        pid=int(pid),
                                        click_delay=click_delay,
                                        times=self._execution_count(rule),
                                        block_user_mouse_move=block_user_mouse_move,
                                    )
                                    used.append((idx, rule))
                                finally:
                                    with self._state_lock:
                                        self._active_action = None
                                        self._active_action_row_id = ""
                                        self._active_action_chain = ()
                        if used:
                            self._mark_used(uid, int(pid), used)
                            did_any = True
                            self._log(
                                f"[Auto-Actions] {uid}: played "
                                + ", ".join(str(rule.name) for _idx, rule in used)
                                + (f" (biome={biome})" if biome else "")
                            )
                    except _MenuGateBlocked as e:
                        self._log(f"[Auto-Actions] {uid}: skipped; {e}.")
                    except Exception as e:
                        self._log(f"[Auto-Actions] {uid}: error during action playback: {e}")
                    finally:
                        if self._post_action_hook is not None:
                            try:
                                self._post_action_hook(str(uid), int(pid))
                            except Exception:
                                pass

                    time.sleep(max(0.05, click_delay))
            finally:
                if paused and self._resume_antiafk:
                    try:
                        self._resume_antiafk()
                    except Exception:
                        pass

            try:
                self._dispatch_next_due_alert(
                    cfg,
                    users,
                    click_delay=click_delay,
                    parsed_rules=all_rules,
                    user_filters=user_filters,
                )
            except Exception as e:
                try:
                    self._log(f"[Auto-Actions] alert dispatch error: {e}")
                except Exception:
                    pass

            try:
                if not self._enabled_snapshot():
                    continue
            except Exception:
                pass

            sleep_for = max(0.2, interval if did_any else min(2.0, interval))
            self._stop.wait(timeout=sleep_for)

        self._log("[Auto-Actions] Engine stopped.")

    def test_once(
        self,
        uid: str,
        row_indices: Optional[Sequence[int]] = None,
        *,
        target_pid: Optional[int] = None,
        target_hwnd: Optional[int] = None,
    ) -> bool:
        if autoit is None:
            self._log("[Auto-Actions] Test: AutoIt is required for input.")
            return False

        cfg = self._cfg_snapshot()
        rules = self._rules_from_cfg(cfg, uid=uid)
        if row_indices is not None:
            wanted = {int(idx) for idx in row_indices}
            rules = [(idx, rule) for idx, rule in rules if int(idx) in wanted]
        if not rules:
            self._log("[Auto-Actions] Test: no actions configured. Add or select at least one action row.")
            return False

        try:
            pid = int(target_pid) if target_pid is not None else self._pid_provider(str(uid))
        except Exception:
            pid = None
        if not pid:
            self._log("[Auto-Actions] Test: could not resolve PID for selected user.")
            return False

        try:
            hwnd = int(target_hwnd) if target_hwnd is not None else self._hwnd_provider(int(pid))
        except Exception:
            hwnd = None
        if not hwnd:
            self._log("[Auto-Actions] Test: could not resolve Roblox window handle for selected user.")
            return False

        if self._in_menu_provider is not None:
            try:
                in_menu = self._in_menu_provider(str(uid))
            except Exception:
                in_menu = None
            if in_menu is None or bool(in_menu):
                self._log("[Auto-Actions] Test: user appears to be in the main menu (or status unknown); skipping.")
                return False

        click_delay = float(cfg.get("click_delay", 0.2) or 0.2)
        block_user_mouse_move = bool(cfg.get("disable_mouse_move", False))
        menu_debug = bool(cfg.get("menu_debug", False))

        paused = False
        try:
            if self._pause_antiafk:
                try:
                    pause_res = self._pause_antiafk()
                except Exception:
                    self._log("[Auto-Actions] Test: failed to pause Anti-AFK; aborting test.")
                    return False
                if pause_res is False:
                    self._log("[Auto-Actions] Test: Anti-AFK pause was denied; aborting test.")
                    return False
                paused = True

            self._log(f"[Auto-Actions] Test: running {len(rules)} selected action row(s) once for {uid}...")
            with self._action_lock:
                for _idx, rule in rules:
                    with self._state_lock:
                        self._active_action = (str(uid), int(_idx))
                        self._active_action_row_id = str(rule.row_id or "")
                        self._active_action_chain = (
                            (str(rule.row_id),) if str(rule.row_id or "").strip() else ()
                        )
                    try:
                        if menu_debug:
                            self._log_menu_activation_debug(
                                uid=str(uid),
                                pid=int(pid),
                                hwnd=int(hwnd),
                                biome="",
                                row_index=int(_idx),
                                rule=rule,
                                min_not_in_menu_s=float(rule.startup_delay_s),
                                phase="test-activate",
                            )
                        self._run_rule_on_window(
                            int(hwnd),
                            rule,
                            uid=str(uid),
                            pid=int(pid),
                            click_delay=click_delay,
                            times=1,
                            block_user_mouse_move=block_user_mouse_move,
                        )
                    finally:
                        with self._state_lock:
                            self._active_action = None
                            self._active_action_row_id = ""
                            self._active_action_chain = ()
            self._log("[Auto-Actions] Test: complete.")
            return True
        except _MenuGateBlocked as e:
            self._log(f"[Auto-Actions] Test: skipped; {e}.")
            return False
        except Exception as e:
            self._log(f"[Auto-Actions] Test: error: {e}")
            return False
        finally:
            if paused and self._resume_antiafk:
                try:
                    self._resume_antiafk()
                except Exception:
                    pass
