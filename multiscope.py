# multiscope.py - MultiScopeEngine with strict switching + live cache refresh
# Strict mapping only: username markers are required; fallback is always explicit.
# Filesystem events enqueue index refreshes; metadata polling is the fallback.
# Affected paths are re-resolved independently from GUI status ticks.
# One-second metadata fallback; every changed attached generation is tailed fairly.
# Generation selection is ranked by the filename session timestamp.
# Biome detection from [BloxstrapRPC] JSON (largeImage.hoverText)
# Merchant detection independent of biomes
# Embeds: 4 rows (Account / Detected by / Time / Private Server)
# Biome Started includes PS link; Biome Ended shows PS label only
# Handoff: previous biome Ended carried donor -> spare

from __future__ import annotations
import base64
import hashlib
import os, re, json, time, threading, requests
import requests.exceptions as _rq_exc
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple
from concurrent.futures import ThreadPoolExecutor
from log_utils import (
    RobloxLogIndex,
    find_log_for_username,
    R_DISC_REASON, R_DISC_NOTIFY, R_DISC_SENDING, R_CONN_LOST,
)

_ACCESS_ENV_NAME = "".join(chr(c) for c in (74, 65, 82, 65, 77, 95, 85, 78, 76, 79, 67, 75))
_ACCESS_MARKER_NAME = "".join(chr(c) for c in (74, 65, 82, 65, 77, 46, 98, 105, 117))

# Optional biomes metadata (color, thumbnail). Fallbacks if missing.
try:
    from biomes import load_biomes_catalog, biome_meta, biome_names
    load_biomes_catalog()
except Exception:
    def biome_meta(name: str) -> Tuple[int, str]:
        return int(0x3BA55D), ""     # default color, empty thumbnail
    def biome_names() -> list[str]:
        return ["NORMAL"]
APP_FOOTER = "J.JARAM JX 2x86"
HARD_EVERYONE_BIOMES = {"GLITCHED", "DREAMSPACE", "CYBERSPACE"}
_LOOKUP_SAVE_LOCK = threading.Lock()
# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def _post_webhook(url: str, payload: dict) -> None:
    if not url:
        return
    try:
        import requests
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass

def _normalize_role_ping_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    mention_match = re.search(r"<@&\s*(\d+)\s*>", text)
    if mention_match:
        return mention_match.group(1)
    id_match = re.search(r"\d+", text)
    return id_match.group(0) if id_match else ""

def _normalize_user_filter_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"blacklist", "blocklist", "exclude", "denylist", "deny"}:
        return "blacklist"
    return "whitelist"

# Parse [BloxstrapRPC] JSON blobs strictly
_LOG_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")
_R_RPC_MARK = "[BloxstrapRPC]"
_R_JSON_START = re.compile(r"\{")
_CURSOR_ANCHOR_BYTES = 256


def _generation_identity_without_revision(generation_id: object) -> str:
    """Return the stable path/filesystem-identity portion of a generation ID."""
    value = str(generation_id or "")
    head, separator, revision = value.rpartition(":")
    if separator and revision.isdigit() and "|" in head:
        return head
    return value


def _cursor_anchor(path: object, pos: object, *, length: int = _CURSOR_ANCHOR_BYTES) -> tuple[int, str]:
    """Hash a bounded byte window immediately before a saved cursor."""
    path_s = str(path or "")
    pos_i = max(0, int(pos or 0))
    take = min(max(0, int(length)), pos_i)
    if not path_s or take <= 0:
        return 0, ""
    with open(path_s, "rb") as handle:
        handle.seek(pos_i - take)
        data = handle.read(take)
    if len(data) != take:
        return 0, ""
    return take, hashlib.sha256(data).hexdigest()

def _parse_log_ts_epoch(ts_text: str) -> Optional[float]:
    try:
        return float(datetime.fromisoformat(str(ts_text or "").replace("Z", "+00:00")).timestamp())
    except Exception:
        return None

def _extract_log_ts_epoch_before(text: str, marker_index: int) -> Optional[float]:
    try:
        line_start = text.rfind("\n", 0, max(0, int(marker_index))) + 1
        prefix = text[line_start:max(0, int(marker_index))]
        m = _LOG_TS_RE.search(prefix)
        if not m:
            return None
        return _parse_log_ts_epoch(m.group(0))
    except Exception:
        return None

def _extract_rpc_entries_from_text(
    text: str,
    *,
    timestamp_hint: Optional[float] = None,
    extract_timestamp: bool = True,
) -> List[Tuple[dict, Optional[float]]]:
    out: List[Tuple[dict, Optional[float]]] = []
    decoder = json.JSONDecoder()
    start = 0
    while True:
        i = text.find(_R_RPC_MARK, start)
        if i == -1:
            break
        m = _R_JSON_START.search(text, i + len(_R_RPC_MARK))
        if not m:
            start = i + len(_R_RPC_MARK)
            continue
        j = m.start()
        try:
            rpc, consumed = decoder.raw_decode(text[j:])
        except Exception:
            start = i + len(_R_RPC_MARK)
            continue
        if isinstance(rpc, dict):
            ts_epoch = _extract_log_ts_epoch_before(text, i) if extract_timestamp else timestamp_hint
            out.append((rpc, ts_epoch))
        start = j + max(1, int(consumed))
    return out

def _extract_rpc_jsons_from_text(text: str) -> List[dict]:
    return [rpc for rpc, _ts in _extract_rpc_entries_from_text(text)]

def _extract_biome_from_rpc(rpc: dict) -> Optional[str]:
    """STRICT: use data.largeImage.hoverText only."""
    if not isinstance(rpc, dict):
        return None
    data = rpc.get("data")
    if not isinstance(data, dict):
        return None
    li = data.get("largeImage")
    if not isinstance(li, dict):
        return None
    biome = li.get("hoverText")
    if isinstance(biome, str):
        biome = biome.strip()
    return biome or None


def _extract_in_menu_from_rpc(rpc: dict) -> Optional[bool]:
    """
    Return True if RPC state mentions the main menu, False if state exists and
    is not the main menu, None if state missing.
    """
    if not isinstance(rpc, dict):
        return None
    data = rpc.get("data")
    if not isinstance(data, dict):
        return None
    texts: List[str] = []

    # Bloxstrap schemas can differ; try both "state" and "details".
    for k in ("state", "details"):
        try:
            v = data.get(k)
        except Exception:
            continue
        if isinstance(v, str) and v.strip():
            texts.append(v.strip())
            continue
        if isinstance(v, dict):
            for kk in ("text", "value", "name", "label", "title"):
                try:
                    vv = v.get(kk)
                except Exception:
                    continue
                if isinstance(vv, str) and vv.strip():
                    texts.append(vv.strip())
                    break

    if not texts:
        return None

    s = " ".join(texts).strip().lower()
    if not s:
        return None

    # Treat common variants as "in menu".
    if ("in main menu" in s) or ("main menu" in s) or ("in menu" in s) or (s in {"menu", "mainmenu"}):
        return True
    return False



# Merchant detection modes.
MERCHANT_MODE_ASSET_ID = "asset_id"
MERCHANT_MODE_LEGACY_CHAT = "legacy_chat"

MERCHANT_ASSET_IDS = {
    "18247420806": "Jester",
    "18247165978": "Mari",
    "97148159887178": "Rin",
}

# Legacy merchant chat lines - tolerant to optional colon after [Merchant]
# and variable ms precision.
MERCHANT_RE = re.compile(
    r"^(?P<full_line>"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{0,6})?Z),"  # allow 0-6 ms digits
    r"[^\n]*?\[(?:Merchant|Merchants)\]:?\s*"                               # optional colon after [Merchant]
    r"(?P<merchant_name>Jester|Mari|Rin)\b"
    r"[^\n]*?\b(arrived|spawn(?:ed|ing)?|appeared)\b"
    r"[^\n]*"
    r")$",
    re.IGNORECASE | re.MULTILINE
)

# Merchant asset-id lines - only the animation asset ID matters; the
# Workspace.Map.* segment is intentionally ignored because it is random.
MERCHANT_ASSET_RE = re.compile(
    r"^(?P<full_line>"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{0,6})?Z),"
    r"[^\n]*?rbxassetid://(?P<asset_id>97148159887178|18247420806|18247165978)"
    r"[^\n]*"
    r")$",
    re.IGNORECASE | re.MULTILINE
)

MERCHANT_ASSET_PREFILTERS = tuple(f"rbxassetid://{asset_id}".lower() for asset_id in MERCHANT_ASSET_IDS)
MERCHANT_LEGACY_PREFILTERS = ("[merchant]", "[merchants]")


def _normalize_merchant_detection_mode(mode: object) -> str:
    raw = str(mode or "").strip().lower()
    if raw in {"legacy", "chat", "merchant", "merchant_chat", MERCHANT_MODE_LEGACY_CHAT}:
        return MERCHANT_MODE_LEGACY_CHAT
    return MERCHANT_MODE_ASSET_ID


def _merchant_prefilters_for_mode(mode: object) -> tuple[str, ...]:
    if _normalize_merchant_detection_mode(mode) == MERCHANT_MODE_LEGACY_CHAT:
        return MERCHANT_LEGACY_PREFILTERS
    return MERCHANT_ASSET_PREFILTERS


def _iter_merchant_matches(text: str, mode: object) -> List[dict]:
    normalized_mode = _normalize_merchant_detection_mode(mode)
    if normalized_mode == MERCHANT_MODE_LEGACY_CHAT:
        return [
            {
                "full_line": m.group("full_line"),
                "timestamp": m.group("timestamp"),
                "merchant_name": m.group("merchant_name").title(),
            }
            for m in MERCHANT_RE.finditer(text)
        ]

    out: List[dict] = []
    for m in MERCHANT_ASSET_RE.finditer(text):
        asset_id = str(m.group("asset_id") or "").strip()
        who = MERCHANT_ASSET_IDS.get(asset_id)
        if not who:
            continue
        out.append(
            {
                "full_line": m.group("full_line"),
                "timestamp": m.group("timestamp"),
                "merchant_name": who,
            }
        )
    return out

# Biome RPC lines - anchor timestamp exactly like merchants
BIOME_RPC_RE = re.compile(
    r"^(?P<full_line>"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z)"
    r".*?\[BloxstrapRPC\]\s*(?P<json>\{.*?\})"
    r")$",
    re.IGNORECASE | re.MULTILINE
)


# ------------------------------------------------------------------------------
# Blocker
# ------------------------------------------------------------------------------

class _TempBlockSession(threading.Thread):
    """
    3-minute temp blocker for one 'finder' account.
    - Uses Roblox user-blocking API (no browser)
    - Tails their log for 'Player added: <name> <id>'
    - Blocks only names present in Blank 
    - Grows lookups/blocklist via Bloxlink reverse search
    - Unblocks any IDs we blocked when the window ends
    """
    GUILD_ID = "1371698242886307921"   # your server (can be moved to credentials if you prefer)
    WINDOW_SEC = 180

    def __init__(self, log_fn, uid: str, username: str, cookie: str):
        super().__init__(daemon=True)
        self._log = log_fn
        self.uid = str(uid)
        self.username = str(username or "").strip()
        self.cookie = cookie
        self._stop = False
        self._blocked_ids = set()
        self._seen_ids = set()
        self._pending: list[tuple[str, str]] = []  # (username, roblox_id) carried across ticks
        self._log_carry = b""

    # ---------- Jaram files ----------
    @staticmethod
    def _jaram_dir() -> Path:
        base = os.environ.get("APPDATA") or ""
        p = Path(base) / "Jaram" if base else Path.cwd() / "Jaram"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def _lookup_path(cls) -> Path:
        return cls._jaram_dir() / "lookup.json"

    @classmethod
    def _cred_path(cls) -> Path:
        return cls._jaram_dir() / "credentials.json"

    @classmethod
    def _load_lookup(cls) -> dict:
        p = cls._lookup_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
        return {"lookups": {}, "blocklist": []}

    @classmethod
    def _save_lookup(cls, obj: dict) -> None:
        p = cls._lookup_path()
        # Use a unique tmp name per thread to avoid collisions, still guarded by a lock
        tmp = p.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        with _LOOKUP_SAVE_LOCK:
            tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
            # Atomic replace on Windows 10+ and modern Python
            tmp.replace(p)


    @classmethod
    def _bloxlink_key(cls) -> str:
        p = cls._cred_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8")) or {}
            return str(data.get("bloxlink_api_key") or "")
        except Exception:
            return ""

    # ---------- Decisions ----------
    @staticmethod
    def _in_blocklist(name: str, lk: dict) -> bool:
        bl = [str(x).lower() for x in (lk.get("blocklist") or [])]
        return name.lower() in bl

    @staticmethod
    def _in_lookups(name: str, lk: dict) -> bool:
        nm = name.lower()
        for arr in (lk.get("lookups") or {}).values():
            for item in arr or []:
                if str(item).lower() == nm:
                    return True
        return False

    @classmethod
    def _add_to_lookups(cls, lk: dict, discord_id: str, username: str) -> None:
        lookups = lk.setdefault("lookups", {})
        arr = lookups.setdefault(str(discord_id), [])
        if username not in arr and username.lower() not in [a.lower() for a in arr]:
            arr.append(username)

    @classmethod
    def _append_blocklist(cls, lk: dict, username: str) -> None:
        bl = lk.setdefault("blocklist", [])
        if username not in bl and username.lower() not in [b.lower() for b in bl]:
            bl.append(username)

    # ---------- Bloxlink reverse ----------
    def _bloxlink_reverse(self, roblox_id: str) -> str | None:
        """
        Map Roblox userId -> Discord ID via Bloxlink using *plain* Authorization header.
        Supports response shapes:
          - {"user":{"id":"..."}}
          - {"discordID":"..."} or {"discordId":"..."}
          - {"discordIDs":["..."]}    a plural form (pick first)
        Returns Discord ID string or None. Logs status so you can verify it ran.
        """
        key = self._bloxlink_key()
        if not key:
            self._log("[TempBlock] Bloxlink key missing; reverse lookup skipped")
            return None

        guild_id = self.GUILD_ID
        url = f"https://api.blox.link/v4/public/guilds/{guild_id}/roblox-to-discord/{roblox_id}"
        headers = {"Authorization": key}

        # 3 tries total, short timeouts, quick backoff on transient failures
        timeouts = (5, 5, 5)  # seconds per attempt
        backoffs = (0.5, 1) # between attempts
        for i, to in enumerate(timeouts):
            try:
                r = requests.get(url, headers=headers, timeout=to)
                status = r.status_code
                try:
                    data = r.json()
                except Exception:
                    data = None

                self._log(f"[TempBlock] Bloxlink - {status} for {roblox_id} (guild {guild_id})")

                if isinstance(data, dict) and data.get("error"):
                    self._log(f"[TempBlock] Bloxlink error: {data.get('error')}")

                if status == 200 and isinstance(data, dict):
                    # 1) {"user":{"id":"..."}}
                    if isinstance(data.get("user"), dict) and data["user"].get("id"):
                        return str(data["user"]["id"])
                    # 2) {"discordID":"..."} / {"discordId":"..."}
                    if data.get("discordID"):
                        return str(data["discordID"])
                    if data.get("discordId"):
                        return str(data["discordId"])
                    # 3) {"discordIDs":["..."]}
                    if isinstance(data.get("discordIDs"), list) and data["discordIDs"]:
                        return str(data["discordIDs"][0])
                    # 200 but unknown shape - treat as no mapping
                    return None

                # 204/404/400/etc - no mapping; don't retry
                if status not in (500, 502, 503, 504):   # only retry server errors
                    return None

            except (_rq_exc.Timeout, _rq_exc.ConnectionError) as e:
                self._log(f"[TempBlock] Bloxlink timeout/network error (try {i+1}/3): {e}")
            except Exception as e:
                self._log(f"[TempBlock] Bloxlink exception (try {i+1}/3): {e}")

            # retry if we have remaining attempts
            if i < len(backoffs):
                time.sleep(backoffs[i])

        # fell through without success
        self._log("[TempBlock] Bloxlink: giving up after retries")
        return None

    # ---------- Roblox user-blocking API ----------
    def _make_session(self):
        from utilities_tab import _make_blocking_api_session  # lazy-import to avoid init cycles
        return _make_blocking_api_session(self.cookie)

    def _block_id(self, session, user_id: str) -> str:
        from utilities_tab import _api_block_user
        return _api_block_user(session, self.cookie, user_id)

    def _unblock_id(self, session, user_id: str) -> str:
        from utilities_tab import _api_unblock_user
        return _api_unblock_user(session, self.cookie, user_id)

    # ---------- Tail the finder's log ----------
    def _tail_new_players(self, f) -> list[tuple[str, str]]:
        """Read any new lines and return [(username, id), ...] that match."""
        found = []
        raw = f.read(256 * 1024)
        if not raw:
            return found
        combined = self._log_carry + raw
        parts = combined.split(b"\n")
        self._log_carry = parts.pop() if parts else combined
        if len(self._log_carry) > 1024 * 1024:
            self._log_carry = b""
        for line_raw in parts:
            line = line_raw.rstrip(b"\r").decode("utf-8", errors="replace")
            if "Player added:" not in line:
                continue
            m = re.search(r"Player added:\s+([A-Za-z0-9_]+)\s+(\d+)", line)
            if m:
                uname = m.group(1)
                rid = m.group(2)
                found.append((uname, rid))
        if self._log_carry:
            tail = self._log_carry.decode("utf-8", errors="replace")
            match = re.search(r"Player added:\s+([A-Za-z0-9_]+)\s+(\d+)", tail)
            if match:
                found.append((match.group(1), match.group(2)))
                self._log_carry = b""
        return found

    # ---------- Main ----------
    def run(self):
        if not self.cookie or not self.username:
            self._log(f"[TempBlock] {self.uid}: missing cookie/username")
            return

        # Create API session up front (CSRF + browserid)
        try:
            session = self._make_session()
        except Exception as e:
            self._log(f"[TempBlock] {self.uid}: failed to start API session ({e})")
            return

        # Prepare log tail
        log_path = find_log_for_username(self.username.lower(), allow_fallback=False)
        if not log_path or not os.path.isfile(log_path):
            self._log(f"[TempBlock] {self.uid}: no log for '{self.username}'")
            try: session.close()
            except Exception: pass
            return

        try:
            f = open(log_path, "rb")
        except Exception as e:
            self._log(f"[TempBlock] {self.uid}: cannot open log ({e})")
            try: session.close()
            except Exception: pass
            return

        # Seek to end so we only see new players after the spawn
        try:
            f.seek(0, os.SEEK_END)
        except Exception:
            pass

        # Load lookup once; we'll persist changes as they occur
        lookup = self._load_lookup()

        deadline = time.time() + self.WINDOW_SEC
        self._log(f"[TempBlock] {self.uid}: window OPEN ({self.WINDOW_SEC}s)")

        try:
            while time.time() < deadline and not self._stop:
                # --- SURGE-AWARE PER-TICK LOGIC ---
                tick_deadline = time.time() + 0.10  # ~100ms budget per tick

                # merge any leftover work from the previous tick with fresh arrivals
                new_players = self._tail_new_players(f)
                batch = (self._pending + new_players)
                self._pending = []

                surge = len(batch) >= 20  # tune threshold as you like
                if surge:
                    self._log(f"[TempBlock] {self.uid}: SURGE mode ({len(batch)} new) - skipping Bloxlink")

                for uname, rid in batch:
                    # budget guard FIRST: if we're out of time, queue this work for next tick
                    if time.time() > tick_deadline:
                        self._pending.append((uname, rid))
                        continue

                    if rid in self._seen_ids:
                        continue

                    # only mark "seen" once we *know* we'll process this entry now
                    # (prevents losing users when we run out of budget)
                    # --- Known bad - block immediately ---
                    if self._in_blocklist(uname, lookup):
                        res = self._block_id(session, rid)
                        if res in ("blocked", "already_blocked"):
                            self._blocked_ids.add(rid)
                            self._log(f"[TempBlock] blocked @{uname} ({rid}) on {self.uid} - {res} [blocklist]")
                        else:
                            self._log(f"[TempBlock] failed blocking @{uname} ({rid}) on {self.uid} - {res} [blocklist]")
                        self._seen_ids.add(rid)
                        continue

                    # --- Already mapped - skip ---
                    if self._in_lookups(uname, lookup):
                        self._log(f"[TempBlock] @{uname} already mapped in lookups - skip")
                        self._seen_ids.add(rid)
                        continue

                    if surge:
                        # SURGE path: skip Bloxlink; block now
                        self._append_blocklist(lookup, uname)
                        self._save_lookup(lookup)
                        self._log(f"[TempBlock] (SURGE) @{uname} - added to blocklist & blocking now")
                        res = self._block_id(session, rid)
                        if res in ("blocked", "already_blocked"):
                            self._blocked_ids.add(rid)
                            self._log(f"[TempBlock] (SURGE) blocked @{uname} ({rid}) on {self.uid} - {res}")
                        else:
                            self._log(f"[TempBlock] (SURGE) failed blocking @{uname} ({rid}) on {self.uid} - {res}")
                        self._seen_ids.add(rid)
                        continue

                    # Normal path: Bloxlink quick try; else block+record
                    self._log(f"[TempBlock] Bloxlink reverse lookup for @{uname} ({rid})")
                    d_id = self._bloxlink_reverse(rid)

                    if d_id:
                        self._add_to_lookups(lookup, d_id, uname)
                        self._save_lookup(lookup)
                        self._log(f"[TempBlock] @{uname} - Discord {d_id} (added to lookups)")
                        # no block
                    else:
                        self._append_blocklist(lookup, uname)
                        self._save_lookup(lookup)
                        self._log(f"[TempBlock] @{uname} - no Bloxlink match; added to blocklist and blocking now")
                        res = self._block_id(session, rid)
                        if res in ("blocked", "already_blocked"):
                            self._blocked_ids.add(rid)
                            self._log(f"[TempBlock] blocked @{uname} ({rid}) on {self.uid} - {res} [unknown-blocklist]")
                        else:
                            self._log(f"[TempBlock] failed blocking @{uname} ({rid}) on {self.uid} - {res} [unknown-blocklist]")

                    # we actually processed this entry this tick - safe to mark seen
                    self._seen_ids.add(rid)
                    
                time.sleep(0.25)

        except Exception as e:
            self._log(f"[TempBlock] {self.uid}: loop crashed - {e!r}")
        finally:
            # Always close the log file
            try:
                f.close()
            except Exception:
                pass

        # Unblock everyone we blocked during this window
        if self._blocked_ids:
            self._log(f"[TempBlock] {self.uid}: window CLOSED - unblocking {len(self._blocked_ids)} id(s)")
            for rid in list(self._blocked_ids):
                res = self._unblock_id(session, rid)
                if res in ("unblocked", "already_unblocked"):
                    self._log(f"[TempBlock] unblocked {rid} - {res}")
                else:
                    self._log(f"[TempBlock] unblock failed {rid} - {res}")
        else:
            self._log(f"[TempBlock] {self.uid}: window CLOSED - nothing to unblock")

        try: session.close()
        except Exception: pass

# ------------------------------------------------------------------------------
# Data
# ------------------------------------------------------------------------------


@dataclass
class ServerScope:
    key: str
    users: Set[str] = field(default_factory=set)
    last_biome: Optional[str] = None
    last_biome_ts: float = 0.0
    last_merchant: Optional[str] = None
    last_merchant_ts: float = 0.0
    in_menu: Optional[bool] = None  # unknown until proven otherwise
    last_menu_ts: float = 0.0
    in_menu_by_uid: Dict[str, Optional[bool]] = field(default_factory=dict)
    last_menu_ts_by_uid: Dict[str, float] = field(default_factory=dict)
    events: int = 0

@dataclass
class Cursor:
    path: Optional[str] = None
    pos: int = 0
    carry: bytes = b""
    generation_id: str = ""
    session_started_at: float = 0.0
    observed_size: int = 0
    last_event_ts: float = 0.0
    dropping_oversized: bool = False

# ------------------------------------------------------------------------------
# Engine
# ------------------------------------------------------------------------------

class MultiScopeEngine:
    def _normalized_server_key(self, uid: str) -> str:
        label = (self._get_server_label(uid) or "").strip()
        u = label.upper()
        if u.startswith("DISCONNECTED") or u.startswith("OFFLINE"):
            return "Disconnected"  # single, friendly pool name
        if u.startswith("PUBLIC:"):
            return f"{label} #{uid}"
        return label or f"Unknown #{uid}"

    def __init__(
        self,
        *,
        get_username: Callable[[str], str],
        get_server_label: Callable[[str], str],
        get_ps_link_for_user: Optional[Callable[[str], str]] = None,
        get_server_owner_for_user: Optional[Callable[[str], str]] = None,  # supplied by GUI
        get_cookie_for_user,            # NEW
        stats_path: Optional[str] = None,
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self._get_username = get_username
        self._get_server_label = get_server_label
        self._get_ps_link = get_ps_link_for_user or (lambda uid: "")
        self._get_owner = get_server_owner_for_user or (lambda uid: "")
        self._get_cookie_for_user = get_cookie_for_user   # NEW
        self._log = log_fn or (lambda _msg: None)

        self._cur: Dict[str, Cursor] = {}
        self._tracked_uids: Set[str] = set()
        # A configured account is not necessarily running. Once status (or a
        # disconnect record) proves that an account is inactive, do not let
        # autonomous discovery attach it to another stale log generation.
        # Existing cursors are allowed to consume a final post-exit flush.
        self._log_resolution_suspended_uids: Set[str] = set()
        self._scopes: Dict[str, ServerScope] = {}
        self._log_index = RobloxLogIndex(enable_watcher=True)
        self._ever_attached_uids: Set[str] = set()
        self._retired_generations_by_uid: Dict[str, Set[str]] = {}
        self._next_path_resolve_mono = 0.0
        self._tail_rotation = 0
        self._reader_stats = {
            "bytes_read": 0,
            "lines_read": 0,
            "decode_errors": 0,
            "oversized_lines": 0,
            "read_failures": 0,
            "truncations": 0,
            "tail_cycles": 0,
            "last_cycle_tailed_users": 0,
            "backlog_users": 0,
        }
        self._reader_stats_lock = threading.Lock()

        # Handoff: donor_uid - spare_uid
        self._handoffs: Dict[str, str] = {}
        # Carry donor's last biome into spare to emit Ended
        self._handoff_prev_biome_for_spare: Dict[str, str] = {}

        # Biome cadence per server
        self._biome_min_interval = 2.0
        self._last_biome_post_by_scope: Dict[str, float] = {}

        # Merchant cadence
        self._merchant_rate_limit = 15.0
        self._last_merchant_post = 0.0
        self._merchant_hook: str = ""
        self._merchant_filters = {"Jester": True, "Mari": True, "Rin": True}
        self._ping_map = {"Jester": "", "Mari": "", "Rin": ""}
        self._merchant_detection_mode = MERCHANT_MODE_ASSET_ID
        self._disable_log_based_merchant_detection = False

        self._first_merchant_scan_done: Set[str] = set()
                
        # Merchant last-post timestamp per scope - merchant - epoch seconds
        self._last_merchant_ts_by_scope: Dict[str, Dict[str, float]] = {}

        # Webhooks
        self._biome_webhooks: List[dict] = []
        self._skip_webhook_unknown_context = False

        self._lock = threading.Lock()
        # Events (thread-safe): GUI will drain these and act (e.g., recycle on disconnect)
        self._event_lock = threading.Lock()
        self._events: list[tuple[str, str, str]] = []   # (kind, uid, payload)

        # Disconnect dedupe: uid -> (normpath, absolute_end_offset)
        self._last_disconnect_sig_by_uid: Dict[str, Tuple[str, int]] = {}
        self._seen_event_ranges: Dict[Tuple[str, int, int, str], None] = {}
        self._seen_event_range_limit = 8192
        self._event_claim_lock = threading.RLock()

        # Status snapshot for lookback gates (set in tick()).
        self._status_snapshot: Dict[str, dict] = {}
        self._status_snapshot_ts: float = 0.0

        # Persistent "found" counters (biomes + merchants)
        self._stats_path = str(stats_path or "").strip()
        self._stats_lock = threading.Lock()
        self._found_stats: dict = self._default_found_stats()
        self._load_found_stats()
        self._ensure_found_stats_catalog()

        # Tail log files concurrently. A narrow per-scope lock preserves the
        # ordering of biome/merchant/disconnect records for users which share a
        # server without serializing ordinary log parsing.
        # At 100 active logs, 64 workers trims the normal small-append cycle
        # without materially changing the heavier parsing case.  Jobs are
        # still bounded here so a large account list cannot create a thread
        # per user.
        self._tail_workers = 64
        self._tail_executor = ThreadPoolExecutor(
            max_workers=self._tail_workers,
            thread_name_prefix="ms-tail",
        )
        self._tail_scope_locks: Dict[str, threading.RLock] = {}
        self._tail_scope_locks_lock = threading.Lock()

        # Webhook delivery remains off the reader pool.
        self._send_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ms-send")

        self._per_read_cap = 256 * 1024
        self._global_read_cap = 4 * 1024 * 1024
        self._max_line_bytes = 1024 * 1024
        
        self._menu_unknown_log_by_uid: Dict[str, str] = {}
        # Disconnect fallback: if in_menu stays unknown too long, recycle that user.
        self._menu_none_since_by_uid: Dict[str, float] = {}
        self._menu_none_timeout_since_by_uid: Dict[str, float] = {}
        self._menu_none_disconnect_fired_by_uid: Set[str] = set()
        self._in_menu_none_timeout_seconds = 120.0
        # Latest live-process creation time observed for each account.  This is
        # deliberately runtime-only: a resumed/new process must prove its own
        # menu state instead of inheriting it from a snapshot or prior log.
        self._process_generation_by_uid: Dict[str, float] = {}
        
        # NEW: per-biome notifier modes (biome -> "None" | "Message" | "Everyone")
        self._biome_modes: Dict[str, str] = {}
        # Raw user-provided biome modes (without enforced overrides).
        self._biome_modes_user: Dict[str, str] = {}
        # Latch relaxed state once found; avoid flapping back to locked.
        self._bm_relaxed: bool = False
        # Require confirmation before forcing locked mode.
        self._bm_lock_confirmed: bool = False
        # Track which biomes were forced to Everyone due to lock enforcement.
        self._lock_forced_biomes: Set[str] = set()

        self._temp_block_sessions = {}  # uid -> expiry epoch (simple gate)
        self._temp_block_disabled: bool = True

    # -- Config ----------------------------------------------------------------

    def configure_webhooks(
        self,
        biome_webhooks: List[dict],
        merchant_hook: str = "",
        enable_jester: bool = True,
        enable_mari: bool = True,
        enable_rin: bool = True,
        jester_ping: str = "",
        mari_ping: str = "",
        rin_ping: str = "",
        merchant_detection_mode: str = MERCHANT_MODE_ASSET_ID,
        disable_log_based_merchant_detection: bool = False,
        merchant_rate_limit: float = 15.0,   # kept for backward-compat; ignored
        biome_min_interval: float = 2.0,
        # NEW:
        biome_modes: Optional[Dict[str, str]] = None,
        skip_webhook_unknown_context: bool = False,
        in_menu_none_timeout_seconds: float = 120.0,
    ) -> None:
        lock_enforced = self._is_bm_lock_enforced()
        lock_disabled = not lock_enforced
        base_modes_raw: Dict[str, str] = {str(k).upper(): str(v) for k, v in (biome_modes or {}).items()}
        base_modes: Dict[str, str] = dict(base_modes_raw)
        forced_biomes: Set[str] = set()
        if lock_enforced:
            for hard in HARD_EVERYONE_BIOMES:
                base_modes[hard] = "Everyone"
                forced_biomes.add(hard)

        normalized_hooks: List[dict] = []
        for wh in (biome_webhooks or []):
            if not isinstance(wh, dict):
                continue
            url = (wh.get("url") or "").strip()
            if not url:
                continue
            allowed_biomes = [
                str(b).upper() for b in (wh.get("biomes") or []) if str(b).strip()
            ]
            modes = {str(k).upper(): str(v) for k, v in (wh.get("biome_modes") or {}).items()}
            if not allowed_biomes and modes:
                allowed_biomes = [k for k, v in modes.items() if str(v).lower() in ("message", "everyone")]
            if not lock_disabled:
                for hard in HARD_EVERYONE_BIOMES:
                    if modes.get(hard) != "Everyone":
                        modes[hard] = "Everyone"
            role_pings: Dict[str, str] = {}
            raw_role_pings = wh.get("biome_role_pings") or wh.get("role_pings") or {}
            if isinstance(raw_role_pings, dict):
                for k, v in raw_role_pings.items():
                    bkey = str(k).strip().upper()
                    if not bkey or bkey in HARD_EVERYONE_BIOMES:
                        continue
                    role_id = _normalize_role_ping_id(v)
                    if role_id:
                        role_pings[bkey] = role_id
            # NEW: user routing
            raw_users = wh.get("users", None)
            users_explicit = bool(wh.get("users_explicit", False))
            users: Optional[List[str]]
            if users_explicit and isinstance(raw_users, (list, tuple, set)):
                users = [str(u).strip() for u in raw_users if str(u).strip()]
            else:
                # None -> no user filter (all users allowed)
                users = None
            user_filter_mode = _normalize_user_filter_mode(wh.get("user_filter_mode", "whitelist"))

            nh = {
                "url": url,
                "name": wh.get("name", ""),
                "biomes": allowed_biomes,
                "biome_modes": modes,
                "biome_role_pings": role_pings,
                "users": users,
                "user_filter_mode": user_filter_mode,
            }
            if users is not None:
                nh["_user_lower"] = {u.lower() for u in users}
            normalized_hooks.append(nh)

        self._biome_webhooks = normalized_hooks
        self._merchant_hook = (merchant_hook or "").strip()
        self._merchant_filters = {
            "Jester": bool(enable_jester),
            "Mari": bool(enable_mari),
            "Rin": bool(enable_rin),
        }
        self._ping_map = {"Jester": jester_ping or "", "Mari": mari_ping or "", "Rin": rin_ping or ""}
        self._merchant_detection_mode = _normalize_merchant_detection_mode(merchant_detection_mode)
        self._disable_log_based_merchant_detection = bool(disable_log_based_merchant_detection)
        # --- ignore merchant_rate_limit entirely (no cooldown)
        self._biome_min_interval = float(biome_min_interval or 2.0)
        self._biome_modes = base_modes
        self._biome_modes_user = base_modes_raw
        self._bm_relaxed = lock_disabled
        self._bm_lock_confirmed = not lock_disabled
        self._lock_forced_biomes = forced_biomes
        self._skip_webhook_unknown_context = bool(skip_webhook_unknown_context)
        try:
            self._in_menu_none_timeout_seconds = max(
                1.0,
                min(86_400.0, float(in_menu_none_timeout_seconds)),
            )
        except (TypeError, ValueError, OverflowError):
            self._in_menu_none_timeout_seconds = 120.0


    # -- Persistent "found" counters -------------------------------------------

    def _default_found_stats(self) -> dict:
        return {
            "schema": 2,
            "biomes_total": {},       # biome -> count (ALL TIME)
            "merchants_total": {},    # merchant -> count (ALL TIME)
            "biome_events": [],       # [{ts: float, biome: str}] (rolling, for 24h/week/month)
            "merchant_events": [],    # [{ts: float, merchant: str}] (rolling, for 24h/week/month)
        }

    def _ensure_found_stats_catalog(self) -> None:
        """
        Ensure the persisted stats file always contains:
        - Every biome from biomes.json (excluding NORMAL) with at least a 0 count
        - The canonical merchants (Jester/Mari/Rin) with at least a 0 count
        """
        path = getattr(self, "_stats_path", "") or ""
        if not path:
            return

        try:
            file_exists = os.path.isfile(path)
        except Exception:
            file_exists = False

        try:
            biomes = [b for b in biome_names() if str(b).strip().upper() != "NORMAL"]
        except Exception:
            biomes = []
        biomes = [str(b).strip().upper() for b in biomes if str(b).strip()]

        with self._stats_lock:
            changed = False

            if self._found_stats.get("schema") != 2:
                self._found_stats["schema"] = 2
                changed = True

            bt = self._found_stats.setdefault("biomes_total", {})
            if not isinstance(bt, dict):
                bt = {}
                self._found_stats["biomes_total"] = bt
                changed = True
            if "NORMAL" in bt:
                try:
                    bt.pop("NORMAL", None)
                    changed = True
                except Exception:
                    pass
            for b in biomes:
                if b not in bt:
                    bt[b] = 0
                    changed = True

            mt = self._found_stats.setdefault("merchants_total", {})
            if not isinstance(mt, dict):
                mt = {}
                self._found_stats["merchants_total"] = mt
                changed = True
            for merch in ("Jester", "Mari", "Rin"):
                if merch not in mt:
                    mt[merch] = 0
                    changed = True

            if not isinstance(self._found_stats.get("biome_events"), list):
                self._found_stats["biome_events"] = []
                changed = True
            if not isinstance(self._found_stats.get("merchant_events"), list):
                self._found_stats["merchant_events"] = []
                changed = True

            self._prune_found_events_locked(now_ts=time.time())

            if changed or not file_exists:
                self._save_found_stats_locked()

    def _prune_found_events_locked(self, *, now_ts: Optional[float] = None) -> None:
        try:
            events = self._found_stats.get("biome_events")
            if not isinstance(events, list):
                self._found_stats["biome_events"] = []
                events = []

            now_v = float(now_ts if now_ts is not None else time.time())
            # Keep ~31 days so "month" (30d) always has coverage.
            cutoff = now_v - (31 * 24 * 3600)

            kept = []
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                ts_raw = ev.get("ts")
                biome_raw = ev.get("biome")
                try:
                    ts_f = float(ts_raw)
                except Exception:
                    continue
                if ts_f < cutoff:
                    continue
                if not isinstance(biome_raw, str):
                    continue
                b = biome_raw.strip().upper()
                if not b or b == "NORMAL":
                    continue
                kept.append({"ts": ts_f, "biome": b})

            kept.sort(key=lambda d: d.get("ts", 0.0))
            MAX_EVENTS = 20_000
            if len(kept) > MAX_EVENTS:
                kept = kept[-MAX_EVENTS:]

            self._found_stats["biome_events"] = kept
        except Exception:
            self._found_stats["biome_events"] = []

        try:
            events = self._found_stats.get("merchant_events")
            if not isinstance(events, list):
                self._found_stats["merchant_events"] = []
                events = []

            now_v = float(now_ts if now_ts is not None else time.time())
            cutoff = now_v - (31 * 24 * 3600)

            kept = []
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                ts_raw = ev.get("ts")
                merch_raw = ev.get("merchant")
                try:
                    ts_f = float(ts_raw)
                except Exception:
                    continue
                if ts_f < cutoff:
                    continue
                if not isinstance(merch_raw, str):
                    continue
                m = merch_raw.strip().title()
                if not m:
                    continue
                kept.append({"ts": ts_f, "merchant": m})

            kept.sort(key=lambda d: d.get("ts", 0.0))
            MAX_EVENTS = 20_000
            if len(kept) > MAX_EVENTS:
                kept = kept[-MAX_EVENTS:]

            self._found_stats["merchant_events"] = kept
        except Exception:
            self._found_stats["merchant_events"] = []

    def _load_found_stats(self) -> None:
        path = getattr(self, "_stats_path", "") or ""
        if not path:
            return
        try:
            if not os.path.isfile(path):
                return
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                raw = json.load(f)
        except Exception:
            return
        if not isinstance(raw, dict):
            return

        stats = self._default_found_stats()

        bt = raw.get("biomes_total")
        if isinstance(bt, dict):
            for k, v in bt.items():
                if not isinstance(k, str):
                    continue
                b = k.strip().upper()
                if not b:
                    continue
                try:
                    stats["biomes_total"][b] = int(v)
                except Exception:
                    continue

        mt = raw.get("merchants_total")
        if isinstance(mt, dict):
            for k, v in mt.items():
                if not isinstance(k, str):
                    continue
                m = k.strip().title()
                if not m:
                    continue
                try:
                    stats["merchants_total"][m] = int(v)
                except Exception:
                    continue

        evs = raw.get("biome_events")
        if isinstance(evs, list):
            stats["biome_events"] = evs
        mevs = raw.get("merchant_events")
        if isinstance(mevs, list):
            stats["merchant_events"] = mevs

        with self._stats_lock:
            self._found_stats = stats
            self._prune_found_events_locked(now_ts=time.time())

    def _save_found_stats_locked(self) -> None:
        path = getattr(self, "_stats_path", "") or ""
        if not path:
            return
        try:
            dirpath = os.path.dirname(path)
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
        except Exception:
            pass

        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._found_stats, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _record_found_biome(self, biome: str, *, ts_epoch: Optional[float] = None) -> None:
        b = str(biome or "").strip().upper()
        if not b or b == "NORMAL":
            return
        try:
            ts = float(ts_epoch if ts_epoch is not None else time.time())
        except Exception:
            ts = time.time()

        with self._stats_lock:
            bt = self._found_stats.setdefault("biomes_total", {})
            try:
                bt[b] = int(bt.get(b, 0)) + 1
            except Exception:
                bt[b] = 1

            self._found_stats.setdefault("biome_events", []).append({"ts": ts, "biome": b})
            self._prune_found_events_locked(now_ts=ts)
            self._save_found_stats_locked()

    def _record_found_merchant(self, merchant: str, *, ts_epoch: Optional[float] = None) -> None:
        m = str(merchant or "").strip().title()
        if not m:
            return
        try:
            ts = float(ts_epoch if ts_epoch is not None else time.time())
        except Exception:
            ts = time.time()
        with self._stats_lock:
            mt = self._found_stats.setdefault("merchants_total", {})
            try:
                mt[m] = int(mt.get(m, 0)) + 1
            except Exception:
                mt[m] = 1
            self._found_stats.setdefault("merchant_events", []).append({"ts": ts, "merchant": m})
            self._prune_found_events_locked(now_ts=ts)
            self._save_found_stats_locked()

    def get_found_stats_snapshot(self) -> dict:
        with self._stats_lock:
            snap = json.loads(json.dumps(self._found_stats))

        bt = snap.get("biomes_total") if isinstance(snap.get("biomes_total"), dict) else {}
        mt = snap.get("merchants_total") if isinstance(snap.get("merchants_total"), dict) else {}
        try:
            biome_total = sum(int(v) for v in bt.values())
        except Exception:
            biome_total = 0
        try:
            merchant_total = sum(int(v) for v in mt.values())
        except Exception:
            merchant_total = 0

        return {
            "biomes_total": bt,
            "merchants_total": mt,
            "biomes_total_count": biome_total,
            "merchants_total_count": merchant_total,
        }

    def get_biomes_found_counts(self, window_seconds: float) -> dict:
        try:
            window = float(window_seconds)
        except Exception:
            window = 0.0
        if window <= 0:
            return {"counts": {}, "total": 0, "window_seconds": window_seconds}

        now_ts = time.time()
        cutoff = now_ts - window
        counts: Dict[str, int] = {}

        with self._stats_lock:
            events = list(self._found_stats.get("biome_events") or [])

        for ev in events:
            if not isinstance(ev, dict):
                continue
            try:
                ts = float(ev.get("ts", 0))
            except Exception:
                continue
            if ts < cutoff:
                continue
            biome = ev.get("biome")
            if not isinstance(biome, str):
                continue
            b = biome.strip().upper()
            if not b or b == "NORMAL":
                continue
            counts[b] = counts.get(b, 0) + 1

        total = sum(counts.values())
        return {"counts": counts, "total": total, "window_seconds": window_seconds}

    def get_merchants_found_counts(self, window_seconds: float) -> dict:
        try:
            window = float(window_seconds)
        except Exception:
            window = 0.0
        if window <= 0:
            return {"counts": {}, "total": 0, "window_seconds": window_seconds}

        now_ts = time.time()
        cutoff = now_ts - window
        counts: Dict[str, int] = {}

        with self._stats_lock:
            events = list(self._found_stats.get("merchant_events") or [])

        for ev in events:
            if not isinstance(ev, dict):
                continue
            try:
                ts = float(ev.get("ts", 0))
            except Exception:
                continue
            if ts < cutoff:
                continue
            merch = ev.get("merchant")
            if not isinstance(merch, str):
                continue
            m = merch.strip().title()
            if not m:
                continue
            counts[m] = counts.get(m, 0) + 1

        total = sum(counts.values())
        return {"counts": counts, "total": total, "window_seconds": window_seconds}


    # -- User mapping / logs ---------------------------------------------------

    def update_users(
        self,
        user_ids: List[str],
        refresh_user_ids: Optional[Set[str]] = None,
    ) -> None:
        user_ids_set = {str(uid) for uid in (user_ids or [])}
        previous_uids = set(self._tracked_uids)
        added_uids = user_ids_set - previous_uids
        refresh_uids = {
            str(uid) for uid in (refresh_user_ids or set()) if str(uid) in user_ids_set
        }
        resolve_uids = added_uids | refresh_uids
        # Discovery and username resolution are expensive. Existing users keep
        # their cursor; only new/renamed users need to be resolved again.
        if resolve_uids:
            self._log_index.poll(force=True)
        self._tracked_uids = set(user_ids_set)
        with self._lock:
            # remove stale only
            stale_uids = {
                str(uid)
                for uid in (
                    set(self._cur.keys())
                    | set(self._retired_generations_by_uid.keys())
                    | set(self._menu_unknown_log_by_uid.keys())
                    | set(self._menu_none_since_by_uid.keys())
                    | set(self._menu_none_timeout_since_by_uid.keys())
                    | set(self._process_generation_by_uid.keys())
                    | set(self._last_disconnect_sig_by_uid.keys())
                    | set(self._log_resolution_suspended_uids)
                    | {
                        str(scope_uid)
                        for scope in self._scopes.values()
                        for scope_uid in scope.users
                    }
                )
                if str(uid) not in user_ids_set
            }
            for uid in stale_uids:
                stale_cur = self._cur.pop(uid, None)
                if stale_cur and stale_cur.generation_id:
                    self._log_index.unpin(stale_cur.generation_id)
                self._retired_generations_by_uid.pop(uid, None)
                self._ever_attached_uids.discard(uid)
                self._menu_unknown_log_by_uid.pop(uid, None)
                self._menu_none_since_by_uid.pop(uid, None)
                self._menu_none_timeout_since_by_uid.pop(uid, None)
                self._process_generation_by_uid.pop(uid, None)
                self._last_disconnect_sig_by_uid.pop(uid, None)
                self._log_resolution_suspended_uids.discard(uid)

        # Do resolves + watcher setup without holding the engine lock
        for uid in resolve_uids:
            self._resolve_current_log(uid, force=True, refresh_index=False)
            cur = self._cur.get(uid)
            if cur and cur.path:
                self._log_index.mark_dirty(cur.path)

        # A tracked account should be visible even before a log is resolved or
        # the first GUI status tick supplies its final server label.
        with self._lock:
            for scope in self._scopes.values():
                for uid in stale_uids:
                    uid_s = str(uid)
                    scope.users.discard(uid_s)
                    scope.in_menu_by_uid.pop(uid_s, None)
                    scope.last_menu_ts_by_uid.pop(uid_s, None)
            self._sync_user_scope_memberships(user_ids_set)

    def _retire_generation(self, uid: str, generation_id: str) -> None:
        generation = str(generation_id or "")
        if not generation:
            return
        uid_s = str(uid)
        retired = self._retired_generations_by_uid.setdefault(uid_s, set())
        retired.add(generation)
        if len(retired) > 8:
            self._retired_generations_by_uid[uid_s] = set(sorted(retired)[-8:])

    def _drop_user_log_tracking(self, uid: str) -> None:
        uid_s = str(uid)
        cur = self._cur.pop(uid_s, None)

        try:
            if cur and cur.generation_id:
                self._log_index.unpin(cur.generation_id)
                self._retire_generation(uid_s, cur.generation_id)
        except Exception:
            pass

    def _resolve_current_log(
        self,
        uid: str,
        *,
        force: bool = False,
        refresh_index: bool = True,
    ) -> None:
        uid = str(uid)
        if uid in self._log_resolution_suspended_uids:
            return
        uname = (self._get_username(uid) or "").strip().lower()
        if not uname:
            return
        not_before = None
        try:
            process_started = self._process_created_at_for_disconnect(uid)
            if process_started is not None:
                not_before = float(process_started) - 15.0
        except Exception:
            not_before = None
        try:
            lookup = self._log_index.lookup(
                uname,
                not_before=not_before,
                refresh_index=refresh_index,
            )
        except Exception:
            return
        match = lookup.match
        if match is None:
            cur = self._cur.get(uid)
            if cur is not None and cur.generation_id and not self._log_index.has_generation(cur.generation_id):
                self._log_index.unpin(cur.generation_id)
                self._retire_generation(uid, cur.generation_id)
                self._cur.pop(uid, None)
                self._mark_menu_unknown(uid)
                return
            if (
                not_before is not None
                and cur is not None
                and float(cur.session_started_at or 0.0) < float(not_before)
            ):
                if cur.generation_id:
                    self._log_index.unpin(cur.generation_id)
                    self._retire_generation(uid, cur.generation_id)
                self._cur.pop(uid, None)
                self._mark_menu_unknown(uid)
            return
        if match.generation_id in self._retired_generations_by_uid.get(uid, set()):
            return
        cur = self._cur.get(uid)
        if cur and cur.generation_id == match.generation_id:
            self._log_index.pin(match)
            cur.path = match.path
            cur.session_started_at = match.session_started_at
            cur.observed_size = int(match.size)
            return
        cold_attach = uid not in self._ever_attached_uids
        if cur and cur.generation_id:
            self._log_index.unpin(cur.generation_id)
            self._retire_generation(uid, cur.generation_id)
            self._mark_menu_unknown(uid)
        next_cursor = Cursor(
            path=match.path,
            pos=int(match.size if cold_attach else 0),
            carry=b"",
            generation_id=match.generation_id,
            session_started_at=float(match.session_started_at),
            observed_size=int(match.size),
        )
        self._cur[uid] = next_cursor
        self._log_index.pin(match)
        self._ever_attached_uids.add(uid)
        self._log_index.mark_dirty(match.path)
        self._log(f"[MultiScope] switched log for {uname} - {os.path.basename(match.path)}")
        if cold_attach:
            self._warmstart_user_tail(uid)

    def _warmstart_user_tail(self, uid: str) -> None:
        cur = self._cur.get(uid)
        if not cur or not cur.path or not os.path.isfile(cur.path):
            return
        try:
            size_now = os.path.getsize(cur.path)
            with open(cur.path, "rb") as f:
                window = 8 * 1024 * 1024
                block_size = 256 * 1024
                base_offset = max(0, int(size_now - window))
                blocks: list[bytes] = []
                read_at = int(size_now)
                while read_at > base_offset:
                    take = min(block_size, read_at - base_offset)
                    read_at -= take
                    f.seek(read_at)
                    blocks.append(f.read(take))
                raw = b"".join(reversed(blocks))
            if base_offset and b"\n" in raw:
                skipped = raw.find(b"\n") + 1
                base_offset += skipped
                raw = raw[skipped:]
            chunk = raw.decode("utf-8", errors="replace")
        except Exception:
            return
        # Cold attachment is a state seed only: never replay historical disconnects.
        disconnect_hit = False

        # merchant seed (no notify) +' seed *scope* timestamps to avoid retro spam across users
        if not self._disable_log_based_merchant_detection:
            matches = _iter_merchant_matches(chunk, self._merchant_detection_mode)
            if matches:
                scope_key = self._server_key_for(uid)
                self._last_merchant_ts_by_scope.setdefault(scope_key, {})
                for m in matches:
                    try:
                        ts = datetime.fromisoformat(str(m.get("timestamp") or "").replace("Z", "+00:00"))
                    except Exception:
                        continue
                    name = str(m.get("merchant_name") or "").title()
                    if not name:
                        continue
                    self._last_merchant_ts_by_scope[scope_key][name] = ts.timestamp()

        # mark this user as warmstarted so its first live read doesn't post old lines
        self._first_merchant_scan_done.add(uid)

        # Seed the current biome/menu without notifying; an unchanged live RPC must
        # not turn startup history into a fresh alert.
        rpc_entries = _extract_rpc_entries_from_text(chunk)
        if rpc_entries:
            latest_biome = None
            latest_biome_ts = None
            for rpc, ts_epoch in reversed(rpc_entries):
                latest_biome = _extract_biome_from_rpc(rpc)
                if latest_biome:
                    latest_biome_ts = ts_epoch
                    break
            key = self._server_key_for(uid)
            scope = self._scope(key)
            scope.users.add(uid)
            if latest_biome:
                scope.last_biome = str(latest_biome).upper()
                if latest_biome_ts is not None:
                    scope.last_biome_ts = float(latest_biome_ts)
                    self._last_biome_post_by_scope[key] = float(latest_biome_ts)
            latest_state = None
            latest_menu_ts: Optional[float] = None
            for rpc, ts_epoch in rpc_entries:
                st = _extract_in_menu_from_rpc(rpc)
                if st is not None and ts_epoch is not None:
                    if latest_menu_ts is None or float(ts_epoch) >= float(latest_menu_ts):
                        latest_state = st
                        latest_menu_ts = float(ts_epoch)
                elif st is not None and latest_menu_ts is None:
                    latest_state = st
            if (not disconnect_hit) and latest_state is not None and latest_menu_ts is not None:
                scope.in_menu = latest_state
                scope.last_menu_ts = float(latest_menu_ts)
                scope.in_menu_by_uid[str(uid)] = latest_state
                scope.last_menu_ts_by_uid[str(uid)] = float(scope.last_menu_ts or 0.0)
                try:
                    self._log(f"[SCAN-TRACE] {uid}: warmstart in_menu={latest_state} menu_ts={scope.last_menu_ts:.3f} server={key}")
                except Exception:
                    pass
            else:
                try:
                    self._log(f"[SCAN-TRACE] {uid}: warmstart no timestamped in_menu found rpc={len(rpc_entries)}")
                except Exception:
                    pass
        cur.pos = int(size_now)
        cur.carry = b""

    def _emit_event(self, kind: str, uid: str, payload: str = "") -> None:
        with self._event_lock:
            self._events.append((kind, uid, payload))

    def drain_events(self):
        with self._event_lock:
            ev = self._events[:]
            self._events.clear()
        return ev

    @staticmethod
    def _recompute_scope_menu(scope: ServerScope) -> None:
        known = []
        for scope_uid in scope.users:
            uid_s = str(scope_uid)
            value = (scope.in_menu_by_uid or {}).get(uid_s, None)
            if value is None:
                continue
            try:
                event_ts = float((scope.last_menu_ts_by_uid or {}).get(uid_s, 0.0) or 0.0)
            except Exception:
                event_ts = 0.0
            known.append((event_ts, uid_s, bool(value)))
        if known:
            event_ts, _uid, value = max(known)
            scope.in_menu = value
            scope.last_menu_ts = event_ts
        else:
            scope.in_menu = None
            scope.last_menu_ts = 0.0

    def _mark_menu_unknown(self, uid: str) -> None:
        try:
            key = self._server_key_for(uid)
            scope = self._scope(key)
            scope.in_menu_by_uid[str(uid)] = None
            scope.last_menu_ts_by_uid[str(uid)] = 0.0
            scope.users.add(uid)
            self._recompute_scope_menu(scope)
        except Exception:
            pass

        try:
            cur = self._cur.get(uid)
            if cur and cur.path:
                self._menu_unknown_log_by_uid[str(uid)] = os.path.abspath(cur.path)
        except Exception:
            pass

    def _clear_menu_unknown(self, uid: str) -> None:
        try:
            self._menu_unknown_log_by_uid.pop(str(uid), None)
        except Exception:
            pass

    def recover_user_log_tracking(
        self,
        uid: str,
        process_created_at: Optional[float] = None,
    ) -> bool:
        """
        Best-effort recovery path for a live user whose strict username->log match
        has reappeared after a disconnect. This clears stale detach state and
        immediately re-resolves/warmstarts the user's current log.
        """
        uid_s = str(uid or "").strip()
        if not uid_s:
            return False
        self._tracked_uids.add(uid_s)

        # Recovery can run over RPC before the next status tick arrives. Seed
        # the new PID time immediately so its warmstart cannot act on a
        # disconnect from the process that was just replaced.
        try:
            created_at = float(process_created_at or 0.0)
        except Exception:
            created_at = 0.0
        if created_at > 0.0:
            try:
                snapshot = dict(self._status_snapshot or {})
                current_status = dict(snapshot.get(uid_s) or {})
                current_status["process_created_at"] = created_at
                snapshot[uid_s] = current_status
                self._status_snapshot = snapshot
                self._process_generation_by_uid[uid_s] = created_at
            except Exception:
                pass

        # This method is called only after the manager has found a strict log
        # for a live launch, so it is authoritative evidence that discovery
        # may resume even if the last status tick still said inactive.
        self._log_resolution_suspended_uids.discard(uid_s)

        try:
            self._menu_none_since_by_uid.pop(uid_s, None)
        except Exception:
            pass
        try:
            self._menu_none_timeout_since_by_uid.pop(uid_s, None)
        except Exception:
            pass
        try:
            self._menu_none_disconnect_fired_by_uid.discard(uid_s)
        except Exception:
            pass
        try:
            self._last_disconnect_sig_by_uid.pop(uid_s, None)
        except Exception:
            pass
        try:
            self._clear_menu_unknown(uid_s)
        except Exception:
            pass

        target_generation = ""
        try:
            uname = str(self._get_username(uid_s) or "").strip().lower()
        except Exception:
            uname = ""
        if uname:
            try:
                lookup = self._log_index.lookup(
                    uname,
                    not_before=(created_at - 15.0) if created_at > 0 else None,
                )
                target_generation = lookup.match.generation_id if lookup.match else ""
            except Exception:
                target_generation = ""

        cur = None
        try:
            cur = self._cur.get(uid_s)
        except Exception:
            cur = None
        already_attached = bool(
            target_generation
            and cur
            and cur.generation_id == target_generation
            and cur.path
            and os.path.isfile(cur.path)
        )
        if already_attached:
            try:
                self._log_index.mark_dirty(cur.path)
            except Exception:
                pass
            return True

        try:
            self._resolve_current_log(uid_s, force=True)
        except Exception:
            return False

        try:
            cur = self._cur.get(uid_s)
            if not (cur and cur.path and os.path.isfile(cur.path)):
                return False
        except Exception:
            return False

        return True

    def _claim_event_range(
        self,
        generation_id: str,
        start: int,
        end: int,
        event_kind: str,
    ) -> bool:
        key = (str(generation_id or ""), max(0, int(start)), max(0, int(end)), str(event_kind))
        with self._event_claim_lock:
            if key in self._seen_event_ranges:
                return False
            self._seen_event_ranges[key] = None
            while len(self._seen_event_ranges) > self._seen_event_range_limit:
                self._seen_event_ranges.pop(next(iter(self._seen_event_ranges)))
            return True

    def _scan_disconnect_in_text(
        self,
        uid: str,
        text: str,
        *,
        path: Optional[str] = None,
        base_offset: int = 0,
        absolute_end: Optional[int] = None,
        absolute_start: Optional[int] = None,
        generation_id: str = "",
        timestamp_hint: Optional[float] = None,
    ) -> bool:
        """Scan `text` for disconnect signals; emits at most once per log position."""
        if not text:
            return False

        # Fast negative check before doing full finditers (keeps behavior close to the old scanner).
        try:
            if not (R_DISC_REASON.search(text) or
                    R_DISC_NOTIFY.search(text) or
                    R_DISC_SENDING.search(text) or
                    R_CONN_LOST.search(text)):
                return False
        except Exception:
            pass

        last_start: Optional[int] = None
        last_end: Optional[int] = None
        last_payload = "detected in log"

        def _consider(match, payload: str) -> None:
            nonlocal last_start, last_end, last_payload
            try:
                start = int(match.start())
                end = int(match.end())
            except Exception:
                return
            if last_end is None or end >= last_end:
                last_start = start
                last_end = end
                last_payload = payload

        try:
            for m in R_DISC_REASON.finditer(text):
                _consider(m, f"reason={m.group(1)}")
        except Exception:
            pass
        try:
            for m in R_DISC_NOTIFY.finditer(text):
                _consider(m, f"reason={m.group(1)}")
        except Exception:
            pass
        try:
            for m in R_DISC_SENDING.finditer(text):
                _consider(m, f"reason={m.group(1)}")
        except Exception:
            pass
        try:
            for m in R_CONN_LOST.finditer(text):
                _consider(m, "connection lost")
        except Exception:
            pass

        if last_start is None or last_end is None:
            return False

        norm_path = ""
        if path:
            try:
                norm_path = os.path.normcase(os.path.abspath(path))
            except Exception:
                norm_path = str(path)

        if absolute_end is not None:
            abs_end = max(0, int(absolute_end))
        else:
            try:
                abs_end = max(0, int(base_offset)) + int(last_end)
            except Exception:
                abs_end = int(last_end)

        prev = self._last_disconnect_sig_by_uid.get(str(uid))
        if prev and prev[0] == norm_path and abs_end <= int(prev[1]):
            return False

        # A warmstart can include a disconnect left in the log by the previous
        # Roblox process. Only let a timestamped line affect the process that
        # was alive when (or before) that line was written.
        disconnect_ts = timestamp_hint
        if disconnect_ts is None:
            disconnect_ts = _extract_log_ts_epoch_before(text, last_start)
        process_created_at = self._process_created_at_for_disconnect(uid)
        if (
            disconnect_ts is not None
            and process_created_at is not None
            and disconnect_ts < process_created_at
        ):
            self._last_disconnect_sig_by_uid[str(uid)] = (norm_path, abs_end)
            return False

        self._last_disconnect_sig_by_uid[str(uid)] = (norm_path, abs_end)
        self._mark_menu_unknown(uid)
        self._log_resolution_suspended_uids.add(str(uid))
        self._drop_user_log_tracking(uid)
        range_start = max(0, int(absolute_start if absolute_start is not None else base_offset))
        if self._claim_event_range(generation_id or norm_path, range_start, abs_end, "disconnect"):
            self._emit_event("disconnect", uid, last_payload)
        return True

    def _scan_disconnect_in_chunk(self, uid: str, chunk: str) -> bool:
        """Check a just-read log chunk for Roblox disconnect signals."""
        return self._scan_disconnect_in_text(uid, chunk)

    def begin_handoff(self, donor_uid: str, spare_uid: str) -> None:
        with self._lock:
            self._handoffs[donor_uid] = spare_uid
            donor_key = self._server_key_for(donor_uid)
            prev = (self._scopes.get(donor_key) or ServerScope(donor_key)).last_biome
            if prev:
                self._handoff_prev_biome_for_spare[spare_uid] = prev
            self._scope(donor_key).users.update({donor_uid, spare_uid})

    def complete_handoff(self, donor_uid: str) -> None:
        with self._lock:
            self._handoffs.pop(donor_uid, None)

    # -- Scope/owner helpers ---------------------------------------------------

    def _server_key_for(self, uid: str) -> str:
        label = (self._get_server_label(uid) or "").strip()
        if not label:
            return f"Unknown #{uid}"
        upper = label.upper()
        if upper.startswith("DISCONNECTED") or upper.startswith("OFFLINE"):
            return "Disconnected"
        if upper.startswith("PUBLIC:"):
            return f"{label} #{uid}"
        return label

    def _display_server_label(self, server_key: str) -> str:
        if not server_key:
            return "Unknown"
        upper = server_key.upper()
        if (upper.startswith("PUBLIC:") or upper.startswith("UNKNOWN #")) and " #" in server_key:
            return server_key.split(" #", 1)[0]
        return server_key

    def _scope(self, key: str) -> ServerScope:
        return self._scopes.setdefault(key, ServerScope(key))

    @staticmethod
    def _is_unresolved_scope_key(key: str) -> bool:
        upper = str(key or "").strip().upper()
        return upper == "UNKNOWN" or upper.startswith("UNKNOWN #")

    def _merge_resolved_scope_state(
        self,
        source_key: str,
        source: ServerScope,
        target_key: str,
        target: ServerScope,
    ) -> None:
        """Move warm-start state only from a UID-isolated unresolved scope."""
        if (
            not self._is_unresolved_scope_key(source_key)
            or self._is_unresolved_scope_key(target_key)
            or str(target_key).strip().lower() == "disconnected"
        ):
            return

        if source.last_biome and float(source.last_biome_ts or 0.0) >= float(target.last_biome_ts or 0.0):
            target.last_biome = source.last_biome
            target.last_biome_ts = float(source.last_biome_ts or 0.0)
        if source.last_merchant and float(source.last_merchant_ts or 0.0) >= float(target.last_merchant_ts or 0.0):
            target.last_merchant = source.last_merchant
            target.last_merchant_ts = float(source.last_merchant_ts or 0.0)

        source_biome_post = self._last_biome_post_by_scope.pop(source_key, None)
        if source_biome_post is not None:
            self._last_biome_post_by_scope[target_key] = max(
                float(self._last_biome_post_by_scope.get(target_key, 0.0) or 0.0),
                float(source_biome_post or 0.0),
            )

        source_merchants = self._last_merchant_ts_by_scope.pop(source_key, {}) or {}
        if source_merchants:
            target_merchants = self._last_merchant_ts_by_scope.setdefault(target_key, {})
            for merchant, event_ts in source_merchants.items():
                target_merchants[str(merchant)] = max(
                    float(target_merchants.get(str(merchant), 0.0) or 0.0),
                    float(event_ts or 0.0),
                )

        target.events += int(source.events or 0)
        source.events = 0

    def _sync_user_scope_memberships(self, user_ids: Iterable[str]) -> None:
        """Move per-user state when process context changes its server scope."""
        desired: Dict[str, str] = {}
        for raw_uid in user_ids:
            uid = str(raw_uid)
            try:
                desired[uid] = self._server_key_for(uid)
            except Exception:
                desired[uid] = f"Unknown #{uid}"

        affected_scopes: Set[str] = set()
        migrated_sources: Set[str] = set()
        ambiguous_legacy_unknown = {
            key
            for key, scope in self._scopes.items()
            if str(key).strip().upper() == "UNKNOWN" and len(scope.users) > 1
        }
        for old_key, old_scope in list(self._scopes.items()):
            for uid in list(old_scope.users):
                new_key = desired.get(str(uid))
                if not new_key or new_key == old_key:
                    continue

                target = self._scope(new_key)
                uid_s = str(uid)
                if old_key not in migrated_sources and old_key not in ambiguous_legacy_unknown:
                    self._merge_resolved_scope_state(old_key, old_scope, new_key, target)
                    migrated_sources.add(old_key)

                if uid_s in (old_scope.in_menu_by_uid or {}):
                    value = old_scope.in_menu_by_uid.pop(uid_s, None)
                    try:
                        menu_ts = float(old_scope.last_menu_ts_by_uid.pop(uid_s, 0.0) or 0.0)
                    except Exception:
                        menu_ts = 0.0

                    try:
                        target_ts = float((target.last_menu_ts_by_uid or {}).get(uid_s, 0.0) or 0.0)
                    except Exception:
                        target_ts = 0.0
                    if uid_s not in target.in_menu_by_uid or menu_ts >= target_ts:
                        target.in_menu_by_uid[uid_s] = value
                        target.last_menu_ts_by_uid[uid_s] = menu_ts
                else:
                    target.in_menu_by_uid.setdefault(uid_s, None)
                    target.last_menu_ts_by_uid.setdefault(uid_s, 0.0)

                old_scope.users.discard(uid)
                target.users.add(uid_s)
                affected_scopes.update({old_key, new_key})

        # An aggregate menu value must not remain owned by a user that moved away.
        for uid, key in desired.items():
            scope = self._scope(key)
            scope.users.add(uid)
            scope.in_menu_by_uid.setdefault(uid, None)
            scope.last_menu_ts_by_uid.setdefault(uid, 0.0)
            affected_scopes.add(key)

        for key in affected_scopes:
            scope = self._scopes.get(key)
            if scope is not None:
                self._recompute_scope_menu(scope)

        # Resolved transient scopes have no independent server identity and
        # should not linger in snapshots or the MultiScope table.
        for key in list(affected_scopes):
            scope = self._scopes.get(key)
            if scope is not None and not scope.users and self._is_unresolved_scope_key(key):
                self._last_biome_post_by_scope.pop(key, None)
                self._last_merchant_ts_by_scope.pop(key, None)
                self._scopes.pop(key, None)

    def _resolve_owner(self, uid: str, server_label: str) -> str:
        """
        Prefer explicit owner callback; otherwise fall back to detector username.
        Kept simple so we don't disturb existing logic elsewhere.
        """
        owner = (self._get_owner(uid) or "").strip()
        if owner:
            return owner
        # fallback: current detecting user
        return (self._get_username(uid) or "Unknown").strip()

    def _should_skip_webhook(self, owner_raw: str, server_label: str, ps_link: str) -> bool:
        if not self._skip_webhook_unknown_context:
            return False
        server_unknown = (not server_label) or server_label.strip().lower() == "unknown"
        owner_unknown = (not owner_raw) or owner_raw.strip().lower() == "unknown"
        ps_unknown = not bool(ps_link)
        if server_unknown or owner_unknown or ps_unknown:
            self._log("[MultiScope] Skipping webhook; owner or private server unknown.")
            return True
        return False

    def _maybe_start_temp_block(self, uid: str, reason: str):
        if self._temp_block_disabled:
            return

        now = time.time()
        exp = self._temp_block_sessions.get(uid, 0)
        if exp > now:
            return  # already running recently

        username = (self._get_username(uid) or "").strip()
        cookie = (self._get_cookie_for_user(uid) or "").strip()
        if not username or not cookie:
            return

        self._log(f"[TempBlock] starting for {uid} ({username}) due to {reason}")
        t = _TempBlockSession(self._log, uid, username, cookie)
        t.start()
        # Gate re-entrancy slightly beyond the window (3min + 30s pad)
        self._temp_block_sessions[uid] = now + (_TempBlockSession.WINDOW_SEC + 30)

    # -- Embeds ----------------------------------------------------------------

    def _build_biome_embed(
        self,
        *,
        event_type: str,      # "start" | "end"
        biome: str,
        owner_name: str,      # PS owner
        detected_by: str,
        server_label: str,
        ps_link: str,
        include_ps_link: bool,
        ts_epoch: Optional[float] = None,   # NEW: anchor to log time
    ) -> dict:
        title = f"🌍 {biome} Biome Started" if event_type == "start" else f"🌍 {biome} Biome Ended"
        color_int, thumb = biome_meta(biome)

        import time as _time
        import datetime as _dt
        unix = int((ts_epoch if ts_epoch is not None else _time.time()))
        iso  = _dt.datetime.fromtimestamp(unix, tz=_dt.timezone.utc).isoformat()

        if include_ps_link and ps_link:
            ps_line = f"**Private Server:** [Private Server Link]({ps_link})"
        else:
            ps_line = f"**Private Server:** `{server_label}`"

        # Long date + exact time WITH seconds
        ts_full = f"<t:{unix}:D> • <t:{unix}:T>"
        ts_rel = f"<t:{unix}:R>"

        description = (
            f"**Owner:** `{owner_name}`\n"
            f"**Detected by:** `{detected_by}`\n"
            f"**Time:** {ts_full}({ts_rel})\n"  # seconds included
            f"{ps_line}"
        )

        embed = {
            "title": title,
            "description": description,
            "color": color_int,
            "timestamp": iso,
        }
        if thumb:
            embed["thumbnail"] = {"url": thumb}

        # Copy merchant's footer style, include server label
        embed["footer"] = {"text": f"{APP_FOOTER}  •  {server_label}"}
        return embed

    def _is_bm_relaxed(self) -> bool:
        try:
            if getattr(self, "_bm_relaxed", False):
                return True
            if os.environ.get(_ACCESS_ENV_NAME, "").strip() == "1":
                return True

            candidates = [Path(_ACCESS_MARKER_NAME)]
            try:
                candidates.append(Path(__file__).resolve().with_name(_ACCESS_MARKER_NAME))
            except Exception:
                pass
            try:
                import sys as _sys
                meipass = getattr(_sys, "_MEIPASS", None)
                if meipass:
                    candidates.append(Path(meipass) / _ACCESS_MARKER_NAME)
            except Exception:
                pass

            for p in candidates:
                try:
                    if p.exists():
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _is_bm_lock_enforced(self) -> bool:
        """
        Returns True when the biome lock should be enforced (force Everyone on hard biomes).
        We double-check before locking to avoid accidental flips during startup/path changes.
        """
        try:
            if self._is_bm_relaxed():
                # If we were previously locked, clear the confirmation so we can relax.
                if getattr(self, "_bm_lock_confirmed", False):
                    self._bm_lock_confirmed = False
                return False

            if getattr(self, "_bm_lock_confirmed", False):
                return True

            # Second pass before enforcing lock to avoid flapping on transient misses.
            if self._is_bm_relaxed():
                self._bm_lock_confirmed = False
                return False

            self._bm_lock_confirmed = True
            return True
        except Exception:
            # Fail closed if anything unexpected happens.
            return True


    def _emit_biome_event(self, uid: str, server_key: str, biome: str, *, event_type: str, ts_epoch: Optional[float] = None) -> None:
        detector     = self._get_username(uid) or uid
        server_label = self._display_server_label(server_key)
        owner_raw    = (self._get_owner(uid) or "").strip()
        owner        = owner_raw or (self._get_username(uid) or "Unknown").strip()
        ps_link      = self._get_ps_link(uid) or ""
        scope        = self._scope(server_key)
        if self._should_skip_webhook(owner_raw, server_label, ps_link):
            return

        b = (biome or "").upper()
        if str(event_type).lower() == "start":
            try:
                self._record_found_biome(b, ts_epoch=ts_epoch)
            except Exception:
                pass
        base_modes = getattr(self, "_biome_modes", {}) or {}
        base_modes_user = getattr(self, "_biome_modes_user", {}) or {}
        lock_disabled = not self._is_bm_lock_enforced()
        forced_biomes = getattr(self, "_lock_forced_biomes", set())

        def _mode_for_hook(hook_modes: Optional[Dict[str, str]]) -> str:
            mode = None
            if isinstance(hook_modes, dict):
                mode = hook_modes.get(b)
            if mode is None:
                mode = base_modes.get(b)
            # If we previously forced Everyone due to lock, drop it once relaxed.
            if lock_disabled and b in forced_biomes:
                if mode is None or str(mode).lower() == "everyone":
                    mode = base_modes_user.get(b)
            if not lock_disabled and b in HARD_EVERYONE_BIOMES:
                return "Everyone"
            if mode is None:
                if lock_disabled and b in HARD_EVERYONE_BIOMES:
                    return "None"
                if b == "NORMAL":
                    return "None"
                return "Message"
            return str(mode).capitalize()

        embed = None

        # Build a set of all known users on this server so that
        # "Assign Users" works per-server, not just for the one
        # account whose log produced this event.
        try:
            server_users = {str(u) for u in getattr(scope, "users", set())}
        except Exception:
            server_users = set()
        server_users.add(str(uid))

        posted_any = False
        for wh in self._biome_webhooks:
            mode = _mode_for_hook(wh.get("biome_modes"))
            if mode == "None":
                continue
            url = (wh.get("url") or "").strip()
            if not url:
                continue
            allowed = wh.get("biomes") or []
            if allowed and b not in allowed:
                continue
            allowed_users = wh.get("users", None)
            # None => no user filter (all users on the server allowed).
            # []   => no users for whitelist, no blocked users for blacklist.
            if allowed_users is not None:
                user_filter_mode = _normalize_user_filter_mode(wh.get("user_filter_mode", "whitelist"))
                if user_filter_mode == "whitelist" and not allowed_users:
                    # Explicitly disabled for all users.
                    continue
                allowed_set = {str(u) for u in allowed_users}
                lower_users = wh.get("_user_lower") or {u.lower() for u in allowed_set}
                uid_str     = str(uid)
                detector_s  = str(detector)
                detector_l  = detector_s.lower()

                # 1) Direct matches: current uid or detector name
                direct_match = (
                    uid_str in allowed_set
                    or detector_s in allowed_set
                    or detector_l in lower_users
                )

                # 2) Server-level matches: any uid on this server
                server_match = any(su in allowed_set for su in server_users)

                user_match = direct_match or server_match
                if user_filter_mode == "blacklist":
                    if user_match:
                        continue
                elif not user_match:
                    continue
            if embed is None:
                embed = self._build_biome_embed(
                    event_type=event_type,
                    biome=b,
                    owner_name=owner,
                    detected_by=detector,
                    server_label=server_label,
                    ps_link=ps_link,
                    include_ps_link=(event_type == "start"),
                    ts_epoch=ts_epoch,  # anchor to log line's timestamp
                )
            content = ""
            if event_type == "start":
                if mode == "Everyone":
                    content = "@everyone"
                else:
                    role_id = _normalize_role_ping_id((wh.get("biome_role_pings") or {}).get(b, ""))
                    if role_id:
                        content = f"<@&{role_id}>"
            payload = {"content": content, "embeds": [embed]}
            try:
                self._send_executor.submit(_post_webhook, url, payload)
            except Exception:
                pass
            posted_any = True

        if posted_any:
            self._log(
                f"[MultiScope] BIOME {event_type.upper()} posted | biome={b} | server={server_key} | "
                f"by={detector} | ts={int(ts_epoch) if ts_epoch else '-'}"
            )
        if posted_any:
            scope.events += 1


    def _emit_merchant(self, uid: str, who: str, event_time_utc: datetime, full_line: str) -> None:
        if self._disable_log_based_merchant_detection:
            return
        try:
            self._record_found_merchant(who, ts_epoch=event_time_utc.timestamp())
        except Exception:
            pass

        server_key   = self._server_key_for(uid)
        scope = self._scopes.setdefault(server_key, ServerScope(server_key))
        scope.last_merchant = who
        try:
            scope.last_merchant_ts = float(event_time_utc.timestamp())
        except Exception:
            scope.last_merchant_ts = time.time()
        scope.users.add(uid)
        self._emit_event("merchant", str(uid), str(who))

        if not self._merchant_hook:
            return
        if not self._merchant_filters.get(who, True):
            return
        server_label = self._display_server_label(server_key)
        detector     = self._get_username(uid) or uid
        owner_raw    = (self._get_owner(uid) or "").strip()
        owner        = owner_raw or (self._get_username(uid) or "Unknown").strip()
        ps_link      = self._get_ps_link(uid) or ""
        if self._should_skip_webhook(owner_raw, server_label, ps_link):
            return

        emojis = {"Jester": "🃏", "Mari": "🛍️", "Rin": "🦊"}
        colors = {"Jester": 0xA352FF, "Mari": 0xFF82AB, "Rin": 0xFF9F1C}
        title  = f"{emojis.get(who,'📣')} {who} Has Arrived!"
        ts     = int(event_time_utc.timestamp())
        ts_full = f"<t:{ts}:D> • <t:{ts}:T>"
        ts_rel  = f"<t:{ts}:R>"

        desc = (
            f"**Owner:** `{owner}`\n"
            f"**Detected by:** `{detector}`\n"
            f"**Detected At:** {ts_full} ({ts_rel})\n"
            f"**Private Server:** " + (f"[Private Server Link]({ps_link})" if ps_link else "`N/A`")
        )

        payload = {"content": (self._ping_map.get(who, "") or ""), "embeds": [{
            "title": title,
            "description": desc,
            "color": colors.get(who, 0x7289DA),
            "timestamp": event_time_utc.isoformat(),
            "footer": {"text": f"{APP_FOOTER}  •  {server_label}"}
        }]}

        try:
            self._send_executor.submit(_post_webhook, self._merchant_hook, payload)
        except Exception:
            pass
        # START TEMP BLOCK WINDOW for Jester
        if who == "Jester":
            self._maybe_start_temp_block(uid, "Jester")

        scope.events += 1

    def record_ocr_merchant(self, uid: str, merchant: str) -> None:
        """
        Update the per-scope "last merchant" tracker from an OCR detection.

        This does not emit/ping webhooks (OCR already does that); it only updates
        MultiScope's last_merchant/age and scope-level dedupe timestamp so the
        MultiScope table + scheduler reflect OCR-found merchants.
        """
        uid = str(uid or "").strip()
        if not uid:
            return

        m = str(merchant or "").strip().lower()
        if m not in ("jester", "mari", "rin"):
            return
        who = m.title()
  
        now_ts = time.time()
        with self._lock:
            scope_key = self._server_key_for(uid)
            scope = self._scope(scope_key)
            try:
                if self._scope_dedupe_merchant_ts(scope_key, who, float(now_ts), window=10.0):
                    return
            except Exception:
                pass
            try:
                self._record_found_merchant(who, ts_epoch=now_ts)
            except Exception:
                pass
            scope.last_merchant = who
            scope.last_merchant_ts = now_ts
            scope.users.add(uid)
            try:
                self._last_merchant_ts_by_scope.setdefault(scope_key, {})[who] = float(now_ts)
            except Exception:
                pass
            self._emit_event("merchant", uid, who)

    def _dispatch_log_line(
        self,
        uid: str,
        line: str,
        *,
        path: str,
        generation_id: str,
        byte_start: int,
        byte_end: int,
        disconnect_already: bool = False,
    ) -> bool:
        lower = line.lower()
        needs_scope_order = bool(
            "[BloxstrapRPC]" in line
            or "disconnect" in lower
            or "connection lost" in lower
            or any(token in lower for token in _merchant_prefilters_for_mode(self._merchant_detection_mode))
        )
        if needs_scope_order:
            with self._tail_scope_lock_for(uid):
                return self._dispatch_log_line_serial(
                    uid,
                    line,
                    path=path,
                    generation_id=generation_id,
                    byte_start=byte_start,
                    byte_end=byte_end,
                    disconnect_already=disconnect_already,
                )
        return self._dispatch_log_line_serial(
            uid,
            line,
            path=path,
            generation_id=generation_id,
            byte_start=byte_start,
            byte_end=byte_end,
            disconnect_already=disconnect_already,
        )

    def _dispatch_log_line_serial(
        self,
        uid: str,
        line: str,
        *,
        path: str,
        generation_id: str,
        byte_start: int,
        byte_end: int,
        disconnect_already: bool = False,
    ) -> bool:
        """Parse one logical line and dispatch each supported record once."""
        ts_epoch: Optional[float] = None
        ts_match = _LOG_TS_RE.search(line)
        if ts_match:
            ts_epoch = _parse_log_ts_epoch(ts_match.group(0))

        disconnect_hit = bool(disconnect_already)
        if not disconnect_hit:
            disconnect_hit = self._scan_disconnect_in_text(
                uid,
                line,
                path=path,
                base_offset=byte_start,
                absolute_start=byte_start,
                absolute_end=byte_end,
                generation_id=generation_id,
                timestamp_hint=ts_epoch,
            )

        lower = line.lower()
        if (
            not self._disable_log_based_merchant_detection
            and ts_epoch is not None
            and any(token in lower for token in _merchant_prefilters_for_mode(self._merchant_detection_mode))
        ):
            matches = _iter_merchant_matches(line.rstrip("\r\n"), self._merchant_detection_mode)
            scope_key = self._server_key_for(uid)
            self._scopes.setdefault(scope_key, ServerScope(scope_key)).users.add(uid)
            for match in matches:
                who = str(match.get("merchant_name") or "").title()
                if not who:
                    continue
                event_dt = datetime.fromtimestamp(float(ts_epoch), tz=timezone.utc)
                if uid not in self._first_merchant_scan_done:
                    self._last_merchant_ts_by_scope.setdefault(scope_key, {})[who] = float(ts_epoch)
                    continue
                claimed = self._claim_event_range(
                    generation_id,
                    byte_start,
                    byte_end,
                    f"merchant:{who.lower()}",
                )
                if claimed and not self._scope_dedupe_merchant_ts(scope_key, who, float(ts_epoch), window=10.0):
                    self._emit_merchant(uid, who, event_dt, str(match.get("full_line") or ""))
            self._first_merchant_scan_done.add(uid)

        if "[BloxstrapRPC]" in line:
            rpc_entries = _extract_rpc_entries_from_text(
                line,
                timestamp_hint=ts_epoch,
                extract_timestamp=False,
            )
            server_key = self._server_key_for(uid)
            scope = self._scopes.setdefault(server_key, ServerScope(server_key))
            scope.users.add(uid)
            for rpc, event_ts in rpc_entries:
                if event_ts is None:
                    continue
                event_ts = float(event_ts)
                menu_state = _extract_in_menu_from_rpc(rpc)
                if menu_state is not None and not disconnect_hit:
                    claimed = self._claim_event_range(
                        generation_id,
                        byte_start,
                        byte_end,
                        "rpc:menu",
                    )
                    # Range claims suppress duplicate scope-level work, but
                    # every attached user still needs its own menu state.
                    if claimed and event_ts >= float(getattr(scope, "last_menu_ts", 0.0) or 0.0):
                        scope.in_menu = menu_state
                        scope.last_menu_ts = event_ts
                    scope.in_menu_by_uid[str(uid)] = menu_state
                    scope.last_menu_ts_by_uid[str(uid)] = event_ts
                    self._clear_menu_unknown(uid)

                biome_name = _extract_biome_from_rpc(rpc)
                if not biome_name:
                    continue
                biome = str(biome_name).upper()
                if not self._claim_event_range(
                    generation_id,
                    byte_start,
                    byte_end,
                    f"rpc:biome:{biome}",
                ):
                    continue
                previous = scope.last_biome
                if not previous and uid in self._handoff_prev_biome_for_spare:
                    previous = self._handoff_prev_biome_for_spare.pop(uid, None)
                if previous and biome == previous:
                    continue
                last_post = float(self._last_biome_post_by_scope.get(server_key, 0.0) or 0.0)
                allow_first = last_post == 0.0 and not previous
                if not allow_first and (event_ts - last_post) < self._biome_min_interval:
                    continue
                if previous:
                    self._emit_biome_event(uid, server_key, previous, event_type="end", ts_epoch=event_ts)
                scope.last_biome = biome
                scope.last_biome_ts = event_ts
                self._last_biome_post_by_scope[server_key] = event_ts
                if biome != "NORMAL":
                    self._emit_biome_event(uid, server_key, biome, event_type="start", ts_epoch=event_ts)
                    if biome in HARD_EVERYONE_BIOMES:
                        self._maybe_start_temp_block(uid, f"Biome:{biome}")
                else:
                    self._log(
                        f"[MultiScope] BIOME START suppressed | biome=NORMAL | "
                        f"user={self._get_username(uid)} | server={server_key}"
                    )

        cur = self._cur.get(uid)
        if cur is not None and ts_epoch is not None:
            cur.last_event_ts = max(float(cur.last_event_ts or 0.0), float(ts_epoch))
        with self._tail_scope_lock_for(uid):
            self._scope(self._server_key_for(uid)).users.add(uid)
        return disconnect_hit

    # -- Tail one user ---------------------------------------------------------

    def _tail_scope_lock_for(self, uid: str) -> threading.RLock:
        try:
            scope_key = str(self._server_key_for(str(uid)) or f"Unknown #{uid}")
        except Exception:
            scope_key = f"Unknown #{uid}"
        with self._tail_scope_locks_lock:
            return self._tail_scope_locks.setdefault(scope_key, threading.RLock())

    def _reader_stat_add(self, key: str, amount: int = 1) -> None:
        with self._reader_stats_lock:
            self._reader_stats[key] = int(self._reader_stats.get(key, 0) or 0) + int(amount)

    def _tail_one(self, uid: str, *, max_bytes: Optional[int] = None) -> int:
        return self._tail_one_serial(uid, max_bytes=max_bytes)

    def _tail_one_serial(self, uid: str, *, max_bytes: Optional[int] = None) -> int:
        cur = self._cur.get(uid)
        if not cur or not cur.path or not os.path.isfile(cur.path):
            self._resolve_current_log(uid, force=True)
            cur = self._cur.get(uid)
            if not cur or not cur.path or not os.path.isfile(cur.path):
                return 0

        disconnect_hit = False
        try:
            start_pos = int(cur.pos or 0)
            size_now = os.path.getsize(cur.path)
            cur.observed_size = int(size_now)
            if size_now < start_pos:
                self._reader_stat_add("truncations")
                self._log_index.mark_dirty(cur.path)
                self._log_index.refresh(force=True)
                self._resolve_current_log(uid, force=True)
                return 0
            if size_now <= start_pos:
                return 0
            limit = self._per_read_cap if max_bytes is None else max(1, int(max_bytes))
            to_read = min(self._per_read_cap, limit, size_now - start_pos)
            with open(cur.path, "rb") as f:
                f.seek(start_pos)
                raw = f.read(to_read)
            if not raw:
                return 0
            cur.pos = start_pos + len(raw)
        except Exception:
            self._reader_stat_add("read_failures")
            return 0

        previous_carry = bytes(cur.carry or b"")
        combined = previous_carry + raw
        combined_base = max(0, start_pos - len(previous_carry))
        if cur.dropping_oversized:
            first_nl = combined.find(b"\n")
            if first_nl < 0:
                cur.carry = b""
                self._reader_stat_add("bytes_read", len(raw))
                return len(raw)
            combined = combined[first_nl + 1:]
            combined_base += first_nl + 1
            previous_carry = b""
            cur.dropping_oversized = False

        nl = combined.rfind(b"\n")
        complete = combined[:nl + 1] if nl >= 0 else b""
        tail = combined[nl + 1:] if nl >= 0 else combined
        cur.carry = tail
        if len(tail) > self._max_line_bytes:
            cur.carry = b""
            cur.dropping_oversized = True
            self._reader_stat_add("oversized_lines")

        line_parts: list[str] = []
        line_offset = int(combined_base)
        disconnect_hit = False
        for line_bytes in complete.splitlines(keepends=True):
            line_end = line_offset + len(line_bytes)
            if len(line_bytes) > self._max_line_bytes:
                self._reader_stat_add("oversized_lines")
                line_offset = line_end
                continue
            try:
                line_text = line_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                line_text = line_bytes.decode("utf-8", errors="replace")
                self._reader_stat_add("decode_errors")
            line_parts.append(line_text)
            disconnect_hit = self._dispatch_log_line(
                uid,
                line_text,
                path=cur.path,
                generation_id=cur.generation_id,
                byte_start=line_offset,
                byte_end=line_end,
                disconnect_already=disconnect_hit,
            )
            line_offset = line_end

        # A final record need not end with a newline. Dispatch it once when one
        # of the supported parsers can prove that the record is complete.
        if cur.carry and len(cur.carry) <= self._max_line_bytes:
            try:
                tail_text = cur.carry.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                tail_text = ""
            tail_is_complete = bool(
                R_DISC_REASON.search(tail_text)
                or R_DISC_NOTIFY.search(tail_text)
                or R_DISC_SENDING.search(tail_text)
                or R_CONN_LOST.search(tail_text)
                or _iter_merchant_matches(tail_text, self._merchant_detection_mode)
                or _extract_rpc_entries_from_text(tail_text)
            )
            if tail_is_complete:
                tail_start = int(cur.pos) - len(cur.carry)
                tail_end = int(cur.pos)
                line_parts.append(tail_text)
                disconnect_hit = self._dispatch_log_line(
                    uid,
                    tail_text,
                    path=cur.path,
                    generation_id=cur.generation_id,
                    byte_start=tail_start,
                    byte_end=tail_end,
                    disconnect_already=disconnect_hit,
                )
                cur.carry = b""

        self._reader_stat_add("bytes_read", len(raw))
        self._reader_stat_add("lines_read", len(line_parts))
        return len(raw)



    def poll_logs(self) -> dict:
        """Advance discovery and tail eligible cursors with bounded concurrency."""
        self._log_index.poll()
        generation_sizes = self._log_index.generation_sizes()
        for cur in tuple(self._cur.values()):
            known_size = generation_sizes.get(cur.generation_id)
            if known_size is not None:
                cur.observed_size = int(known_size)

        now_mono = time.monotonic()
        if now_mono >= self._next_path_resolve_mono:
            self._next_path_resolve_mono = now_mono + 0.25
            for uid in sorted(self._tracked_uids):
                self._resolve_current_log(uid, refresh_index=False)

        uids = sorted(
            uid
            for uid in self._tracked_uids
            if uid in self._cur
            and int(self._cur[uid].observed_size) != int(self._cur[uid].pos)
        )
        if not uids:
            with self._reader_stats_lock:
                self._reader_stats["tail_cycles"] += 1
                self._reader_stats["last_cycle_tailed_users"] = 0
                self._reader_stats["backlog_users"] = 0
            return self.reader_diagnostics()
        start = self._tail_rotation % len(uids)
        ordered = uids[start:] + uids[:start]

        remaining = self._global_read_cap
        jobs: list[tuple[str, int]] = []
        for uid in ordered:
            if remaining <= 0:
                break
            cur = self._cur.get(uid)
            if cur is None:
                continue
            delta = max(1, abs(int(cur.observed_size) - int(cur.pos)))
            reserved = min(self._per_read_cap, remaining, delta)
            jobs.append((uid, reserved))
            remaining -= reserved

        if jobs:
            self._tail_rotation = (start + len(jobs)) % len(uids)
            futures = [
                self._tail_executor.submit(self._tail_one, uid, max_bytes=max_bytes)
                for uid, max_bytes in jobs
            ]
            # Complete the batch before the process handles state-changing
            # commands. This keeps process ownership deterministic while the
            # expensive per-user file reads/parsers run concurrently.
            for future in futures:
                try:
                    future.result()
                except Exception:
                    self._reader_stat_add("read_failures")

        backlog_users = sum(
            1
            for uid in self._tracked_uids
            if uid in self._cur
            and int(self._cur[uid].observed_size) != int(self._cur[uid].pos)
        )
        with self._reader_stats_lock:
            self._reader_stats["tail_cycles"] += 1
            self._reader_stats["last_cycle_tailed_users"] = len(jobs)
            self._reader_stats["backlog_users"] = int(backlog_users)
        return self.reader_diagnostics()

    def reader_diagnostics(self) -> dict:
        with self._reader_stats_lock:
            out = dict(self._reader_stats)
        out["index"] = self._log_index.diagnostics_snapshot()
        out["tracked_users"] = len(self._tracked_uids)
        out["attached_users"] = len(self._cur)
        out["tail_workers"] = int(self._tail_workers)
        return out

    def diagnostics_snapshot(self) -> dict:
        return self.reader_diagnostics()

    # -- Public loop hooks -----------------------------------------------------

    def tick(self, status_by_uid: Dict[str, dict]) -> None:
        with self._lock:
            # Keep a snapshot for lookback gates (used by warmstart on log switches).
            try:
                import time
                self._status_snapshot = status_by_uid or {}
                self._status_snapshot_ts = float(time.time())
            except Exception:
                self._status_snapshot = status_by_uid or {}
                self._status_snapshot_ts = 0.0

            # Context can arrive after autonomous warm-start. Move the user's
            # seeded state out of its UID-isolated unresolved scope (or a
            # previous server) before the
            # old scope is pruned.
            scope_uids = set(str(uid) for uid in self._cur.keys())
            scope_uids.update(str(uid) for uid in (status_by_uid or {}).keys())
            self._sync_user_scope_memberships(scope_uids)

            # Log I/O is autonomous in poll_logs(); ticks only update state/context.
            import time
            now_t = time.time()

            # Disconnect condition: in-menu remains unknown for too long.
            # Skip users already in the disconnected pool.
            try:
                for uid, st in (status_by_uid or {}).items():
                    uid = str(uid)
                    try:
                        key = self._server_key_for(uid)
                    except Exception:
                        key = f"Unknown #{uid}"

                    if key == "Disconnected":
                        self._log_resolution_suspended_uids.add(uid)
                        self._mark_menu_unknown(uid)
                        self._drop_user_log_tracking(uid)
                        self._menu_none_since_by_uid.pop(uid, None)
                        self._menu_none_timeout_since_by_uid.pop(uid, None)
                        self._menu_none_disconnect_fired_by_uid.discard(uid)
                        self._process_generation_by_uid.pop(uid, None)
                        continue

                    pids = []
                    try:
                        if isinstance(st, dict):
                            pids = st.get("pids") or []
                    except Exception:
                        pids = []

                    # Only apply while the user is actually running.
                    if not pids:
                        # A later PID must establish fresh menu state even when
                        # the server label has not reached Disconnected yet.
                        self._log_resolution_suspended_uids.add(uid)
                        self._mark_menu_unknown(uid)
                        self._menu_none_since_by_uid.pop(uid, None)
                        self._menu_none_timeout_since_by_uid.pop(uid, None)
                        self._menu_none_disconnect_fired_by_uid.discard(uid)
                        self._process_generation_by_uid.pop(uid, None)
                        continue

                    # A live PID is the normal lifecycle signal that permits
                    # autonomous discovery again. Strict-log recovery also
                    # clears this latch when it arrives before the next tick.
                    self._log_resolution_suspended_uids.discard(uid)

                    scope = self._scope(key)
                    scope.users.add(uid)

                    try:
                        process_created_at = float(st.get("process_created_at", 0.0) or 0.0)
                    except Exception:
                        process_created_at = 0.0
                    previous_process = self._process_generation_by_uid.get(uid)
                    if process_created_at > 0.0:
                        if (
                            previous_process is not None
                            and abs(float(previous_process) - process_created_at) > 0.001
                        ):
                            # A menu record is evidence about one process/log
                            # generation, never about the account forever.
                            self._mark_menu_unknown(uid)
                            self._menu_none_since_by_uid.pop(uid, None)
                            self._menu_none_timeout_since_by_uid.pop(uid, None)
                            self._menu_none_disconnect_fired_by_uid.discard(uid)
                        self._process_generation_by_uid[uid] = process_created_at

                    scope = self._scope(key)
                    menu_state = (scope.in_menu_by_uid or {}).get(uid, None)
                    try:
                        menu_ts = float((scope.last_menu_ts_by_uid or {}).get(uid, 0.0) or 0.0)
                    except Exception:
                        menu_ts = 0.0

                    # Snapshot/cold-start state is accepted only when the
                    # attached generation and its record can belong to this
                    # process.  This also protects the first tick, where there
                    # is no prior process token to compare against.
                    state_is_current = menu_state is not None
                    cur = self._cur.get(uid)
                    if state_is_current and not (cur and cur.path):
                        state_is_current = False
                    if state_is_current and process_created_at > 0.0:
                        generation_cutoff = process_created_at - 15.0
                        if (
                            menu_ts < generation_cutoff
                            or float(getattr(cur, "session_started_at", 0.0) or 0.0) < generation_cutoff
                        ):
                            state_is_current = False
                    if not state_is_current and menu_state is not None:
                        self._mark_menu_unknown(uid)
                        scope = self._scope(key)

                    if not state_is_current:
                        # Only start the in_menu-none timeout after we have a strict
                        # per-user log attached (username marker found in logs).
                        has_user_log = False
                        try:
                            cur = self._cur.get(uid)
                            has_user_log = bool(cur and cur.path and os.path.isfile(cur.path))
                            if has_user_log and process_created_at > 0.0:
                                has_user_log = (
                                    float(cur.session_started_at or 0.0)
                                    >= process_created_at - 15.0
                                )
                        except Exception:
                            has_user_log = False

                        if not has_user_log:
                            self._menu_none_since_by_uid.pop(uid, None)
                            self._menu_none_timeout_since_by_uid.pop(uid, None)
                            self._menu_none_disconnect_fired_by_uid.discard(uid)
                        else:
                            since = self._menu_none_since_by_uid.get(uid)
                            if since is None:
                                self._menu_none_since_by_uid[uid] = now_t
                                self._menu_none_timeout_since_by_uid.pop(uid, None)
                            else:
                                try:
                                    initial_ready_at = float(since) + 30.0
                                except Exception:
                                    initial_ready_at = now_t + 30.0

                                if now_t < initial_ready_at:
                                    self._menu_none_timeout_since_by_uid.pop(uid, None)
                                else:
                                    try:
                                        antiafk_ts = float(st.get("antiafk_last_action_at", 0.0) or 0.0)
                                    except Exception:
                                        antiafk_ts = 0.0

                                    if antiafk_ts < initial_ready_at:
                                        self._menu_none_timeout_since_by_uid.pop(uid, None)
                                    else:
                                        timeout_since = self._menu_none_timeout_since_by_uid.get(uid)
                                        if timeout_since is None:
                                            timeout_since = max(float(initial_ready_at), float(antiafk_ts))
                                            self._menu_none_timeout_since_by_uid[uid] = timeout_since

                                        if (
                                            (now_t - float(timeout_since))
                                            >= self._in_menu_none_timeout_seconds
                                            and uid not in self._menu_none_disconnect_fired_by_uid
                                        ):
                                            self._mark_menu_unknown(uid)
                                            self._drop_user_log_tracking(uid)
                                            timeout_value = int(self._in_menu_none_timeout_seconds)
                                            self._emit_event(
                                                "disconnect",
                                                uid,
                                                f"in_menu_none_timeout={timeout_value}",
                                            )
                                            self._menu_none_disconnect_fired_by_uid.add(uid)
                    else:
                        self._menu_none_since_by_uid.pop(uid, None)
                        self._menu_none_timeout_since_by_uid.pop(uid, None)
                        self._menu_none_disconnect_fired_by_uid.discard(uid)
            except Exception:
                pass

            # prune quiet, empty scopes (unchanged)
            now_t = time.time()
            for key, scope in list(self._scopes.items()):
                scope.users = {u for u in scope.users if self._server_key_for(u) == key}
                try:
                    scope.in_menu_by_uid = {str(u): v for u, v in (scope.in_menu_by_uid or {}).items() if str(u) in scope.users}
                    scope.last_menu_ts_by_uid = {str(u): float(v or 0.0) for u, v in (scope.last_menu_ts_by_uid or {}).items() if str(u) in scope.users}
                except Exception:
                    pass
                quiet = (now_t - max(scope.last_biome_ts, scope.last_merchant_ts, 0)) > 600
                if not scope.users and quiet:
                    self._scopes.pop(key, None)

    def export_state(self) -> dict:
        """
        Export a JSON-serializable snapshot of MultiScope runtime state.
        Used by GUI Pause/Resume so in-menu + last biome/merchant state isn't reset.
        """
        out: dict = {"version": 2, "ts": time.time()}
        with self._lock:
            try:
                known_uids = set(self._cur.keys())
            except Exception:
                known_uids = set()

            scopes: dict = {}
            for key, s in (self._scopes or {}).items():
                try:
                    k = str(key)
                except Exception:
                    continue

                try:
                    users = [str(u) for u in (s.users or set()) if not known_uids or str(u) in known_uids]
                except Exception:
                    users = []

                try:
                    scopes[k] = {
                        "key": k,
                        "users": users,
                        "last_biome": (str(s.last_biome) if s.last_biome else ""),
                        "last_biome_ts": float(getattr(s, "last_biome_ts", 0.0) or 0.0),
                        "last_merchant": (str(s.last_merchant) if s.last_merchant else ""),
                        "last_merchant_ts": float(getattr(s, "last_merchant_ts", 0.0) or 0.0),
                        "in_menu": (None if getattr(s, "in_menu", None) is None else bool(getattr(s, "in_menu", None))),
                        "last_menu_ts": float(getattr(s, "last_menu_ts", 0.0) or 0.0),
                        "in_menu_by_uid": {
                            str(u): (
                                None
                                if (getattr(s, "in_menu_by_uid", {}) or {}).get(str(u), None) is None
                                else bool((getattr(s, "in_menu_by_uid", {}) or {}).get(str(u), None))
                            )
                            for u in users
                            if str(u) in (getattr(s, "in_menu_by_uid", {}) or {})
                        },
                        "last_menu_ts_by_uid": {
                            str(u): float((getattr(s, "last_menu_ts_by_uid", {}) or {}).get(str(u), 0.0) or 0.0)
                            for u in users
                            if str(u) in (getattr(s, "last_menu_ts_by_uid", {}) or {})
                        },
                        "events": int(getattr(s, "events", 0) or 0),
                    }
                except Exception:
                    continue

            cursors: dict = {}
            for uid, cur in (self._cur or {}).items():
                try:
                    u = str(uid)
                except Exception:
                    continue
                if known_uids and u not in known_uids:
                    continue
                try:
                    stat_now = os.stat(cur.path) if getattr(cur, "path", None) else None
                    cursor_state = {
                        "path": (str(cur.path) if getattr(cur, "path", None) else None),
                        "pos": int(getattr(cur, "pos", 0) or 0),
                        "carry_b64": base64.b64encode(bytes(getattr(cur, "carry", b"") or b"")).decode("ascii"),
                        "generation_id": str(getattr(cur, "generation_id", "") or ""),
                        "session_started_at": float(getattr(cur, "session_started_at", 0.0) or 0.0),
                        "last_event_ts": float(getattr(cur, "last_event_ts", 0.0) or 0.0),
                        "dropping_oversized": bool(getattr(cur, "dropping_oversized", False)),
                        "size_at_snapshot": int(stat_now.st_size) if stat_now is not None else 0,
                        "mtime_ns_at_snapshot": int(
                            getattr(stat_now, "st_mtime_ns", int(stat_now.st_mtime * 1_000_000_000))
                        ) if stat_now is not None else 0,
                    }
                    try:
                        anchor_size, anchor_sha256 = _cursor_anchor(cur.path, cursor_state["pos"])
                        if anchor_size and anchor_sha256:
                            cursor_state["anchor_size"] = anchor_size
                            cursor_state["anchor_sha256"] = anchor_sha256
                    except Exception:
                        pass
                    cursors[u] = cursor_state
                except Exception:
                    continue

            out["scopes"] = scopes
            out["cursors"] = cursors

            try:
                out["handoffs"] = {str(k): str(v) for k, v in (self._handoffs or {}).items()}
            except Exception:
                out["handoffs"] = {}
            try:
                out["handoff_prev_biome_for_spare"] = {
                    str(k): str(v) for k, v in (self._handoff_prev_biome_for_spare or {}).items()
                }
            except Exception:
                out["handoff_prev_biome_for_spare"] = {}

            # Best-effort dedupe/throttle caches (safe to omit if they fail)
            try:
                out["last_biome_post_by_scope"] = {
                    str(k): float(v) for k, v in (self._last_biome_post_by_scope or {}).items()
                }
            except Exception:
                out["last_biome_post_by_scope"] = {}
            try:
                out["last_merchant_ts_by_scope"] = {
                    str(scope): {str(m): float(ts) for m, ts in (mm or {}).items()}
                    for scope, mm in (self._last_merchant_ts_by_scope or {}).items()
                }
            except Exception:
                out["last_merchant_ts_by_scope"] = {}
            try:
                out["first_merchant_scan_done"] = [
                    str(u)
                    for u in (self._first_merchant_scan_done or set())
                    if not known_uids or str(u) in known_uids
                ]
            except Exception:
                out["first_merchant_scan_done"] = []
            try:
                out["last_disconnect_sig_by_uid"] = {
                    str(uid): [str(sig[0]), int(sig[1])]
                    for uid, sig in (self._last_disconnect_sig_by_uid or {}).items()
                    if not known_uids or str(uid) in known_uids
                }
            except Exception:
                out["last_disconnect_sig_by_uid"] = {}
            out["retired_generations_by_uid"] = {
                str(uid): sorted(str(g) for g in generations)
                for uid, generations in self._retired_generations_by_uid.items()
            }
            out["ever_attached_uids"] = sorted(self._ever_attached_uids)

        return out

    def import_state(self, state: dict) -> bool:
        """
        Restore a previously-exported runtime snapshot.
        Returns True if anything was applied.
        """
        if not isinstance(state, dict) or not state:
            return False
        try:
            ver = int(state.get("version", 0) or 0)
        except Exception:
            ver = 0
        if ver not in {1, 2}:
            return False

        applied = False
        with self._lock:
            try:
                known_uids = set(self._cur.keys())
            except Exception:
                known_uids = set()

            # -- Scopes -------------------------------------------------------
            scopes_in = state.get("scopes") or {}
            if isinstance(scopes_in, dict) and scopes_in:
                for key, raw in scopes_in.items():
                    if not isinstance(raw, dict):
                        continue
                    k = str(key)
                    scope = self._scopes.get(k) or ServerScope(k)
                    try:
                        users_raw = raw.get("users") or []
                        if isinstance(users_raw, (list, tuple, set)):
                            scope.users = {str(u) for u in users_raw if not known_uids or str(u) in known_uids}
                    except Exception:
                        pass
                    try:
                        b = str(raw.get("last_biome") or "").strip().upper()
                        scope.last_biome = b or None
                    except Exception:
                        pass
                    try:
                        scope.last_biome_ts = float(raw.get("last_biome_ts", scope.last_biome_ts) or 0.0)
                    except Exception:
                        pass
                    try:
                        m = str(raw.get("last_merchant") or "").strip().title()
                        scope.last_merchant = m or None
                    except Exception:
                        pass
                    try:
                        scope.last_merchant_ts = float(raw.get("last_merchant_ts", scope.last_merchant_ts) or 0.0)
                    except Exception:
                        pass
                    try:
                        val = raw.get("in_menu", None)
                        scope.in_menu = None if val is None else bool(val)
                    except Exception:
                        pass
                    try:
                        scope.last_menu_ts = float(raw.get("last_menu_ts", scope.last_menu_ts) or 0.0)
                    except Exception:
                        pass
                    try:
                        menu_map = raw.get("in_menu_by_uid") or {}
                        if isinstance(menu_map, dict):
                            out = {}
                            for map_uid, map_val in menu_map.items():
                                uid_s = str(map_uid)
                                if scope.users and uid_s not in scope.users:
                                    continue
                                out[uid_s] = None if map_val is None else bool(map_val)
                            scope.in_menu_by_uid = out
                    except Exception:
                        pass
                    try:
                        ts_map = raw.get("last_menu_ts_by_uid") or {}
                        if isinstance(ts_map, dict):
                            out_ts = {}
                            for map_uid, map_ts in ts_map.items():
                                uid_s = str(map_uid)
                                if scope.users and uid_s not in scope.users:
                                    continue
                                try:
                                    out_ts[uid_s] = float(map_ts or 0.0)
                                except Exception:
                                    out_ts[uid_s] = 0.0
                            scope.last_menu_ts_by_uid = out_ts
                    except Exception:
                        pass
                    try:
                        scope.events = int(raw.get("events", scope.events) or 0)
                    except Exception:
                        pass
                    self._scopes[k] = scope
                    applied = True

            # -- Cursors: v1 text offsets are unsafe and intentionally ignored. --
            cursors_in = state.get("cursors") or {}
            if ver == 2 and isinstance(cursors_in, dict) and cursors_in:
                import os
                for uid, raw in cursors_in.items():
                    u = str(uid)
                    if known_uids and u not in known_uids:
                        continue
                    if not isinstance(raw, dict):
                        continue
                    cur = self._cur.get(u)
                    if not cur:
                        continue
                    saved_generation = str(raw.get("generation_id") or "")
                    current_generation = str(getattr(cur, "generation_id", "") or "")
                    try:
                        snap_path = raw.get("path")
                        cur_path = getattr(cur, "path", None)
                        if snap_path and cur_path:
                            sp = os.path.normcase(os.path.abspath(str(snap_path)))
                            cp = os.path.normcase(os.path.abspath(str(cur_path)))
                            same_path = sp == cp
                        elif snap_path or cur_path:
                            same_path = False
                        else:
                            same_path = True
                    except Exception:
                        continue

                    try:
                        stat_now = os.stat(cur.path) if cur.path else None
                        size_now = int(stat_now.st_size) if stat_now is not None else 0
                        if stat_now is None:
                            continue
                        saved_pos = max(0, int(raw.get("pos", getattr(cur, "pos", 0)) or 0))
                        saved_size = int(raw.get("size_at_snapshot", raw.get("pos", 0)) or 0)
                    except Exception:
                        continue

                    saved_identity = _generation_identity_without_revision(saved_generation)
                    current_identity = _generation_identity_without_revision(current_generation)
                    exact_generation = bool(
                        saved_generation
                        and current_generation
                        and saved_generation == current_generation
                    )
                    stable_identity = bool(
                        saved_identity
                        and current_identity
                        and saved_identity == current_identity
                    )

                    # Generation revisions are local to an index instance, so a
                    # restarted child can label the same physical log ":0" even
                    # when the paused snapshot called it ":1". Validate the
                    # bytes immediately before the cursor before deciding that a
                    # mismatch means rotation; otherwise resume can replay every
                    # historical merchant record from byte zero.
                    anchor_size = 0
                    anchor_sha256 = ""
                    anchor_matches = False
                    try:
                        anchor_size = max(0, int(raw.get("anchor_size", 0) or 0))
                        anchor_sha256 = str(raw.get("anchor_sha256") or "").strip().lower()
                        if (
                            same_path
                            and anchor_size > 0
                            and anchor_size <= _CURSOR_ANCHOR_BYTES
                            and saved_pos >= anchor_size
                            and size_now >= saved_pos
                            and anchor_sha256
                        ):
                            actual_size, actual_sha256 = _cursor_anchor(
                                cur.path,
                                saved_pos,
                                length=anchor_size,
                            )
                            anchor_matches = (
                                actual_size == anchor_size
                                and actual_sha256.lower() == anchor_sha256
                            )
                    except Exception:
                        anchor_matches = False

                    has_anchor = bool(anchor_size and anchor_sha256)
                    if has_anchor:
                        content_continues = bool(
                            same_path and size_now >= saved_pos and anchor_matches
                        )
                    else:
                        # Older version-2 snapshots have no anchor. Preserve
                        # their safe same-generation/revision-only resume path.
                        content_continues = bool(
                            same_path
                            and size_now >= saved_pos
                            and (exact_generation or stable_identity)
                        )

                    truncated = bool(
                        same_path and (size_now < saved_pos or size_now < saved_size)
                    )
                    identity_changed = bool(
                        saved_identity
                        and current_identity
                        and saved_identity != current_identity
                    )

                    if content_continues:
                        cur.pos = saved_pos
                    elif truncated or not same_path or identity_changed:
                        # A genuinely new generation is intentionally replayed
                        # from its beginning so events written while paused (and
                        # before username discovery) are not lost.
                        cur.pos = 0
                        cur.carry = b""
                        cur.dropping_oversized = False
                    else:
                        # Ambiguous same-path metadata/content drift must fail
                        # quiet. Replaying an old file is worse than omitting an
                        # unverifiable paused interval, especially for merchants.
                        cur.pos = size_now
                        cur.carry = b""
                        cur.dropping_oversized = False
                    cur.observed_size = size_now

                    try:
                        if content_continues:
                            carry = base64.b64decode(str(raw.get("carry_b64") or ""), validate=True)
                            cur.carry = carry if len(carry) <= self._max_line_bytes else b""
                    except Exception:
                        cur.carry = b""
                    try:
                        cur.last_event_ts = float(raw.get("last_event_ts", 0.0) or 0.0)
                        cur.dropping_oversized = bool(raw.get("dropping_oversized", False))
                    except Exception:
                        pass
                    applied = True

            # -- Handoffs -----------------------------------------------------
            try:
                h = state.get("handoffs") or {}
                if isinstance(h, dict):
                    self._handoffs = {
                        str(k): str(v)
                        for k, v in h.items()
                        if not known_uids or (str(k) in known_uids and str(v) in known_uids)
                    }
                    applied = True
            except Exception:
                pass
            try:
                hb = state.get("handoff_prev_biome_for_spare") or {}
                if isinstance(hb, dict):
                    self._handoff_prev_biome_for_spare = {
                        str(k): str(v) for k, v in hb.items() if not known_uids or str(k) in known_uids
                    }
                    applied = True
            except Exception:
                pass

            # -- Dedupe/throttle caches --------------------------------------
            try:
                lbp = state.get("last_biome_post_by_scope") or {}
                if isinstance(lbp, dict):
                    self._last_biome_post_by_scope = {str(k): float(v) for k, v in lbp.items()}
                    applied = True
            except Exception:
                pass
            try:
                lmt = state.get("last_merchant_ts_by_scope") or {}
                if isinstance(lmt, dict):
                    merged: dict = {}
                    for scope, mm in lmt.items():
                        if not isinstance(mm, dict):
                            continue
                        merged[str(scope)] = {str(m): float(ts) for m, ts in mm.items()}
                    self._last_merchant_ts_by_scope = merged
                    applied = True
            except Exception:
                pass
            try:
                fms = state.get("first_merchant_scan_done") or []
                if isinstance(fms, (list, tuple, set)):
                    self._first_merchant_scan_done = {
                        str(u) for u in fms if not known_uids or str(u) in known_uids
                    }
                    applied = True
            except Exception:
                pass
            try:
                lds = state.get("last_disconnect_sig_by_uid") or {}
                if isinstance(lds, dict):
                    out = {}
                    for uid, sig in lds.items():
                        u = str(uid)
                        if known_uids and u not in known_uids:
                            continue
                        if isinstance(sig, (list, tuple)) and len(sig) == 2:
                            out[u] = (str(sig[0]), int(sig[1]))
                    self._last_disconnect_sig_by_uid = out
                    applied = True
            except Exception:
                pass
            if ver == 2:
                try:
                    retired = state.get("retired_generations_by_uid") or {}
                    if isinstance(retired, dict):
                        self._retired_generations_by_uid = {
                            str(uid): set(sorted(str(g) for g in (generations or []) if str(g))[-8:])
                            for uid, generations in retired.items()
                            if isinstance(generations, (list, tuple, set))
                        }
                    ever = state.get("ever_attached_uids") or []
                    if isinstance(ever, (list, tuple, set)):
                        self._ever_attached_uids.update(str(uid) for uid in ever)
                    applied = True
                except Exception:
                    pass

        return applied

    def snapshot(self) -> List[dict]:
        out: List[dict] = []
        now_t = time.time()
        for key, s in sorted(self._scopes.items(), key=lambda kv: kv[0]):
            server_label = self._display_server_label(key)
            users = sorted(list(s.users))
            in_menu_by_uid = {}
            last_menu_ts_by_uid = {}
            try:
                for uid in users:
                    uid_s = str(uid)
                    if uid_s in (s.in_menu_by_uid or {}):
                        val = (s.in_menu_by_uid or {}).get(uid_s, None)
                        in_menu_by_uid[uid_s] = None if val is None else bool(val)
                    if uid_s in (s.last_menu_ts_by_uid or {}):
                        last_menu_ts_by_uid[uid_s] = float((s.last_menu_ts_by_uid or {}).get(uid_s, 0.0) or 0.0)
            except Exception:
                in_menu_by_uid = {}
                last_menu_ts_by_uid = {}
            out.append({
                "server": server_label,
                "server_key": key,
                "users": users,
                "in_menu": s.in_menu,
                "last_menu_ts": float(getattr(s, "last_menu_ts", 0.0) or 0.0),
                "in_menu_by_uid": in_menu_by_uid,
                "last_menu_ts_by_uid": last_menu_ts_by_uid,
                "last_biome": s.last_biome or "",
                "biome_age": int(now_t - s.last_biome_ts) if s.last_biome_ts else None,
                "last_merchant": s.last_merchant or "",
                "merchant_age": int(now_t - s.last_merchant_ts) if s.last_merchant_ts else None,
                "events": s.events,
            })
        return out
    
    def _throttled_log(self, key: str, msg: str, every: float = 10.0) -> None:
        import time
        if not hasattr(self, "_last_log_by_key"):
            self._last_log_by_key = {}
        now = time.time()
        last = self._last_log_by_key.get(key, 0.0)
        if (now - last) >= every:
            self._last_log_by_key[key] = now
            self._log(msg)

    def _should_disconnect_lookback(self, uid: str) -> bool:
        """
        Guard for disconnect lookback scans:
        - Avoid triggering restarts for idle users when MultiScope starts.
        - Allow for users that are active OR recently active (PID died but log flushed a disconnect).
        """
        try:
            key = self._server_key_for(uid)
            if key == "Disconnected":
                return False
        except Exception:
            pass

        try:
            st = (self._status_snapshot or {}).get(uid)
        except Exception:
            st = None
        if not isinstance(st, dict):
            return False

        try:
            pids = st.get("pids") or []
        except Exception:
            pids = []
        if pids:
            return True

        try:
            last_active = float(st.get("last_active", 0) or 0)
        except Exception:
            last_active = 0.0

        try:
            now_t = float(getattr(self, "_status_snapshot_ts", 0.0) or 0.0) or time.time()
        except Exception:
            now_t = time.time()

        # If the user was active recently, still allow the lookback (disconnect may have been written
        # before we switched to the newest log file, or after the PID already died).
        return bool(last_active and (now_t - last_active) <= 180.0)

    def _process_created_at_for_disconnect(self, uid: str) -> Optional[float]:
        """Return the newest live process creation time supplied by the manager."""
        try:
            st = (self._status_snapshot or {}).get(str(uid))
        except Exception:
            st = None
        if not isinstance(st, dict):
            return None

        try:
            created_at = float(st.get("process_created_at", 0.0) or 0.0)
        except Exception:
            created_at = 0.0
        return created_at if created_at > 0.0 else None
    
    def _scope_dedupe_merchant_ts(self, scope_key: str, merchant: str, ts_epoch: float, window: float = 2.0) -> bool:
        """
        Return True for an already-covered or out-of-order merchant event.
        Otherwise advance the scope watermark and return False.
        """
        with self._event_claim_lock:
            d = self._last_merchant_ts_by_scope.setdefault(scope_key, {})
            last = d.get(merchant)
            # Never move this watermark backward. A restored cursor or a second
            # account on the same server can expose an older log range after a newer
            # merchant was already observed; accepting it would replay the entire
            # historical merchant sequence.
            if last is not None and ts_epoch <= (float(last) + max(0.0, float(window))):
                return True
            d[merchant] = ts_epoch
            return False
    
    # in multiscope.py (inside class MultiScopeEngine)
    def shutdown(self):
        try:
            self._log_index.shutdown()
        except Exception:
            pass
        tail = getattr(self, "_tail_executor", None)
        try:
            if tail:
                tail.shutdown(wait=True, cancel_futures=True)
        except Exception:
            pass
        snd = getattr(self, "_send_executor", None)
        try:
            if snd:
                snd.shutdown(wait=False)
        except Exception:
            pass
        # temp-block sessions currently auto-expire; no explicit cancel hook yet
        try:
            with self._stats_lock:
                self._prune_found_events_locked(now_ts=time.time())
                self._save_found_stats_locked()
        except Exception:
            pass








