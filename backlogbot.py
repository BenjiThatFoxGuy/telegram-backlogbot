import asyncio
import hashlib
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client
from pyrogram.errors import FloodWait
from pymongo import ReturnDocument

load_dotenv()

logger = logging.getLogger("backlogbot")
 

def configure_logging() -> None:
    """Configure logging for backlogbot.

    Goal: keep our own logic logs visible while reducing noisy MTProto/client logs
    from Pyrogram and other dependencies.

    Env vars:
      - BACKLOG_LOG_LEVEL: backlogbot logger level (default: INFO)
      - BACKLOG_LIB_LOG_LEVEL: 3rd-party library logger level (default: WARNING)
    """

    def _level_from_env(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None:
            return default
        raw = raw.strip().upper()
        return getattr(logging, raw, default)

    bot_level = _level_from_env("BACKLOG_LOG_LEVEL", logging.INFO)
    lib_level = _level_from_env("BACKLOG_LIB_LOG_LEVEL", logging.WARNING)

    # Default root to library level so we don't get spam from dependencies.
    # We'll explicitly set backlogbot's logger to bot_level.
    logging.basicConfig(
        level=lib_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Our logger: keep at requested level.
    logger.setLevel(bot_level)

    # Known noisy loggers (MTProto / network chatter / DB driver diagnostics)
    noisy = [
        "pyrogram",
        "pyrogram.raw",
        "pyrogram.connection",
        "pyrogram.session",
        "pyrogram.dispatcher",
        "pyrogram.handlers",
        "pyrogram.parser",
        "pymongo",
        "asyncio",
        # Not used directly here, but some deployments share config across bots.
        "telethon",
        "telethon.network",
        "telethon.client",
    ]
    for name in noisy:
        logging.getLogger(name).setLevel(lib_level)



def _log_env_snapshot() -> None:
    """Log a non-sensitive snapshot of runtime configuration inputs.

    Intentionally avoids printing secrets.
    """
    def _present(name: str) -> str:
        return "set" if os.getenv(name) not in (None, "") else "missing"

    logger.info(
        "Env snapshot: BACKLOG_ENABLE=%r BACKLOG_ROOT=%r BACKLOG_ARCHIVE_ROOT=%r BACKLOG_TARGETS=%r "
        "BACKLOG_STATE_DB=%r MONGO_URL=%s TG_API_ID=%s TG_API_HASH=%s TG_PASSWORD=%s TZ=%r",
        os.getenv("BACKLOG_ENABLE"),
        os.getenv("BACKLOG_ROOT"),
        os.getenv("BACKLOG_ARCHIVE_ROOT"),
        os.getenv("BACKLOG_TARGETS"),
        os.getenv("BACKLOG_STATE_DB"),
        "set" if os.getenv("MONGO_URL") else "missing",
        _present("TG_API_ID"),
        _present("TG_API_HASH"),
        _present("TG_PASSWORD"),
        os.getenv("TZ"),
    )


# -----------------------------
# Env / config
# -----------------------------

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    return int(v)


def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None else str(v)


def parse_duration_to_seconds(value: str) -> int:
    """Parse a compact duration like '30s', '24h', '7d', '2w', '15m'."""
    value = value.strip()
    m = re.fullmatch(r"(?i)\s*(\d+)\s*([smhdw])\s*", value)
    if not m:
        raise ValueError(f"Invalid duration format: {value!r} (expected like 30s, 24h, 7d)")
    n = int(m.group(1))
    unit = m.group(2).lower()
    scale = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return n * scale


@dataclass(frozen=True)
class BacklogConfig:
    enabled: bool
    backlog_root: Path
    backlog_roots: List[Path]
    archive_root: Path

    targets_allowlist: List[str]

    scan_every_seconds: int
    settle_seconds: int

    interval_seconds: int
    scope: str  # per_target|global
    overdue: str  # post_once|wait

    success_action: str  # delete|archive

    allow_unknown_as_document: bool

    # If true, do not quarantine files for non-allowlisted targets; just ignore them.
    skip_quarantine_unmapped_targets: bool

    # Direct-post mode convenience: if true, allow posting one item immediately per target
    # when the process starts, even if BACKLOG_INTERVAL_SECONDS hasn't elapsed yet.
    immediate_post_on_start: bool

    use_telegram_scheduler: bool
    scheduler_mode: str  # fixed_cadence|legacy
    schedule_ahead_seconds: int
    min_schedule_delay_seconds: int

    max_failures: int

    tz_name: str

    mongo_url: str
    backlog_state_db: str


MISSING_FILE_MAX_RETRIES = 10


def load_config() -> BacklogConfig:
    backlog_root = Path(_env_str("BACKLOG_ROOT", "/backlog"))

    # Support multiple input roots via BACKLOG_ROOT_1, BACKLOG_ROOT_2, ...
    # If none are provided, fall back to BACKLOG_ROOT.
    numbered_roots: List[Path] = []
    for i in range(1, 51):
        v = os.getenv(f"BACKLOG_ROOT_{i}")
        if v is None:
            continue
        v = v.strip()
        if not v:
            continue
        numbered_roots.append(Path(v))

    backlog_roots = numbered_roots or [backlog_root]
    archive_root = Path(_env_str("BACKLOG_ARCHIVE_ROOT", "/backlog_archive"))

    allowlist_raw = _env_str("BACKLOG_TARGETS", "").strip()
    targets_allowlist = [t.strip() for t in allowlist_raw.split(",") if t.strip()]

    use_scheduler = _env_bool("BACKLOG_USE_TELEGRAM_SCHEDULER", False)
    schedule_ahead_raw = _env_str("BACKLOG_SCHEDULE_AHEAD", "7d")
    scheduler_mode = _env_str("BACKLOG_SCHEDULER_MODE", "fixed_cadence").strip()

    cfg = BacklogConfig(
        enabled=_env_bool("BACKLOG_ENABLE", True),
        backlog_root=backlog_root,
        backlog_roots=backlog_roots,
        archive_root=archive_root,
        targets_allowlist=targets_allowlist,
        scan_every_seconds=_env_int("BACKLOG_SCAN_EVERY_SECONDS", 30),
        settle_seconds=_env_int("BACKLOG_SETTLE_SECONDS", 30),
        interval_seconds=_env_int("BACKLOG_INTERVAL_SECONDS", 21600),
        scope=_env_str("BACKLOG_SCOPE", "per_target").strip(),
        overdue=_env_str("BACKLOG_OVERDUE", "post_once").strip(),
        success_action=_env_str("BACKLOG_SUCCESS_ACTION", "delete").strip(),
        allow_unknown_as_document=_env_bool("BACKLOG_ALLOW_UNKNOWN_AS_DOCUMENT", False),

        skip_quarantine_unmapped_targets=_env_bool(
            "BACKLOG_SKIP_QUARANTINE_UNMAPPED_TARGETS",
            False,
        ),

        immediate_post_on_start=_env_bool(
            "BACKLOG_IMMEDIATE_POST_ON_START",
            False,
        ),
        use_telegram_scheduler=use_scheduler,
        scheduler_mode=scheduler_mode,
        schedule_ahead_seconds=parse_duration_to_seconds(schedule_ahead_raw),
        min_schedule_delay_seconds=_env_int("BACKLOG_MIN_SCHEDULE_DELAY_SECONDS", 120),
        max_failures=_env_int("BACKLOG_MAX_FAILURES", 5),
        tz_name=_env_str("TZ", "Europe/Warsaw"),
        mongo_url=_env_str("MONGO_URL", "mongodb://localhost:27017"),
        backlog_state_db=_env_str("BACKLOG_STATE_DB", "backlogbot_backlogdata"),
    )

    if cfg.scope not in {"per_target", "global"}:
        raise ValueError("BACKLOG_SCOPE must be 'per_target' or 'global'")
    if cfg.overdue not in {"post_once", "wait"}:
        raise ValueError("BACKLOG_OVERDUE must be 'post_once' or 'wait'")
    if cfg.success_action not in {"delete", "archive"}:
        raise ValueError("BACKLOG_SUCCESS_ACTION must be 'delete' or 'archive'")

    if cfg.use_telegram_scheduler and not schedule_ahead_raw:
        raise ValueError("BACKLOG_SCHEDULE_AHEAD is required when BACKLOG_USE_TELEGRAM_SCHEDULER=true")

    if cfg.scheduler_mode not in {"fixed_cadence", "legacy"}:
        raise ValueError("BACKLOG_SCHEDULER_MODE must be 'fixed_cadence' or 'legacy'")

    logger.info(
        "Config: enabled=%s root=%s roots=%s archive=%s allowlist=%s scan_every=%ss settle=%ss interval=%ss scope=%s overdue=%s "
        "success_action=%s allow_unknown_as_document=%s skip_quarantine_unmapped_targets=%s "
        "immediate_post_on_start=%s scheduler=%s scheduler_mode=%s schedule_ahead=%ss min_schedule_delay=%ss max_failures=%s state_db=%s tz=%s",
        cfg.enabled,
        cfg.backlog_root,
        cfg.backlog_roots,
        cfg.archive_root,
        cfg.targets_allowlist,
        cfg.scan_every_seconds,
        cfg.settle_seconds,
        cfg.interval_seconds,
        cfg.scope,
        cfg.overdue,
        cfg.success_action,
        cfg.allow_unknown_as_document,
        cfg.skip_quarantine_unmapped_targets,
        cfg.immediate_post_on_start,
        cfg.use_telegram_scheduler,
        cfg.scheduler_mode,
        cfg.schedule_ahead_seconds,
        cfg.min_schedule_delay_seconds,
        cfg.max_failures,
        cfg.backlog_state_db,
        cfg.tz_name,
    )

    return cfg


# -----------------------------
# Filetypes
# -----------------------------

FILETYPE_MAP: Dict[str, str] = {
    ".jpg": "photo",
    ".jpeg": "photo",
    ".png": "photo",
    ".mp4": "video",
    ".mov": "video",
    ".webm": "video",
    ".gif": "animation",
    ".webp": "sticker",
    ".tgs": "sticker",
}


def caption_sidecar_for(media_path: Path) -> Path:
    return Path(str(media_path) + ".caption.txt")


def compute_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def as_aware_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except Exception:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def safe_move(src: Path, dst: Path) -> None:
    ensure_dir(dst.parent)
    shutil.move(str(src), str(dst))


def safe_delete(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)  # type: ignore[attr-defined]
    except TypeError:
        # Python <3.8 compatibility (not expected here, but safe)
        try:
            if path.exists():
                path.unlink()
        except OSError as e:
            logger.exception("Failed to delete %s: %s", path, e)
    except OSError as e:
        logger.exception("Failed to delete %s: %s", path, e)


def is_stable_file(path: Path, settle_seconds: int) -> bool:
    try:
        st1 = path.stat()
    except FileNotFoundError:
        return False
    # If very new, wait
    if time.time() - st1.st_mtime < settle_seconds:
        return False
    # Some mounts can update mtime late; do a short double-check
    try:
        st2 = path.stat()
    except FileNotFoundError:
        return False
    return st1.st_size == st2.st_size and st1.st_mtime == st2.st_mtime


# -----------------------------
# Target resolution / allowlist
# -----------------------------

def normalize_target_token(token: str) -> str:
    token = token.strip()
    if not token:
        return token
    # Keep @ form as-is
    if token.startswith("@"):
        return token
    # Numeric-ish
    token = token.replace(" ", "")
    return token


def parse_allowlist(tokens: Iterable[str]) -> List[str]:
    out: List[str] = []
    for t in tokens:
        t = normalize_target_token(t)
        if t:
            out.append(t)
    return out


def sha_marker(sha256: str) -> str:
    return f"\n\n#backlogbot:{sha256}"


def _floodwait_seconds(e: FloodWait) -> int:
    # Pyrogram FloodWait typically exposes `.value` or `.x` depending on version.
    for attr in ("value", "x"):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v
    # Fallback to 0 (caller will likely treat as retry next cycle)
    return 0


# -----------------------------
# Mongo model helpers
# -----------------------------

class BacklogStore:
    def __init__(self, conn: AsyncIOMotorClient, db_name: str):
        self._db = conn[db_name]
        self.targets = self._db["targets"]
        self.items = self._db["items"]
        self.counters = self._db["counters"]

    async def ensure_indexes(self) -> None:
        try:
            await self.items.create_index([("target_key", 1), ("seq", 1)], unique=True)
        except Exception:
            pass
        # Speed up de-dup checks (same content re-appearing under different filenames)
        try:
            await self.items.create_index([("target_key", 1), ("sha256", 1)])
        except Exception:
            pass
        try:
            await self.items.create_index([("sha256", 1)])
        except Exception:
            pass
        try:
            await self.items.create_index([("status", 1), ("next_attempt_at", 1)])
        except Exception:
            pass

    async def find_existing_content_item(self, *, target_key: str, sha256: str) -> Optional[Dict[str, Any]]:
        """Return an existing item with the same content hash for this target.

        Used for de-duplication decisions. Note that callers may choose to quarantine
        only when the existing item is already posted.
        """
        return await self.items.find_one(
            {
                "target_key": target_key,
                "sha256": sha256,
                "status": {"$in": ["pending", "scheduled", "posted"]},
            },
            projection={
                "_id": 1,
                "rel_path": 1,
                "status": 1,
                "posted_message_id": 1,
                "scheduled_message_id": 1,
            },
        )

    async def get_or_create_target(self, target_key: str) -> Dict[str, Any]:
        doc = await self.targets.find_one({"_id": target_key})
        if doc:
            return doc
        doc = {
            "_id": target_key,
            "peer_id": None,
            "last_post_at": None,
            "last_scheduled_at": None,
            # Fixed-cadence scheduler anchor: next schedule slot for this target.
            # When BACKLOG_USE_TELEGRAM_SCHEDULER=true, we schedule items at cadence_next_at,
            # then bump it forward by BACKLOG_INTERVAL_SECONDS. Persisted across restarts.
            "cadence_next_at": None,
            "created_at": now_utc(),
            "updated_at": now_utc(),
        }
        await self.targets.insert_one(doc)
        return doc

    async def set_target_peer_id(self, target_key: str, peer_id: int) -> None:
        await self.targets.update_one(
            {"_id": target_key},
            {"$set": {"peer_id": peer_id, "updated_at": now_utc()}},
            upsert=True,
        )

    async def next_seq(self, target_key: str) -> int:
        res = await self.counters.find_one_and_update(
            {"_id": target_key},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        # res can be None depending on driver behavior; fall back
        if not res:
            doc = await self.counters.find_one({"_id": target_key})
            return int(doc.get("seq", 1))
        return int(res.get("seq", 1))

    async def upsert_item_discovered(
        self,
        *,
        target_key: str,
        rel_path: str,
        sha256: str,
        size: int,
        mtime: float,
        send_kind: str,
    ) -> Optional[Dict[str, Any]]:
        # If already exists with same rel_path, do nothing
        existing = await self.items.find_one({"target_key": target_key, "rel_path": rel_path})
        if existing:
            return None
        seq = await self.next_seq(target_key)
        item = {
            "target_key": target_key,
            "rel_path": rel_path,
            "seq": seq,
            "sha256": sha256,
            "size": size,
            "mtime": mtime,
            "send_kind": send_kind,
            "status": "pending",  # pending|scheduled|posted|failed|quarantined
            "fail_count": 0,
            "next_attempt_at": now_utc(),
            "created_at": now_utc(),
            "updated_at": now_utc(),
            "scheduled_message_id": None,
            "posted_message_id": None,
            "last_error": None,
        }
        await self.items.insert_one(item)
        return item

    async def get_next_due_item(self, *, target_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
        q: Dict[str, Any] = {
            "status": {"$in": ["pending"]},
            "next_attempt_at": {"$lte": now_utc()},
        }
        if target_key is not None:
            q["target_key"] = target_key
        return await self.items.find_one(q, sort=[("seq", 1)])

    async def set_item_status(self, item_id: Any, status: str, **fields: Any) -> None:
        fields = dict(fields)
        fields["status"] = status
        fields["updated_at"] = now_utc()
        await self.items.update_one({"_id": item_id}, {"$set": fields})

    async def mark_local_success_action_applied(self, item_id: Any) -> None:
        await self.items.update_one(
            {"_id": item_id},
            {"$set": {"local_success_action_at": now_utc(), "updated_at": now_utc()}},
        )

    async def bump_failure(self, item_id: Any, error: str, retry_after_seconds: int) -> Dict[str, Any]:
        await self.items.update_one(
            {"_id": item_id},
            {
                "$inc": {"fail_count": 1},
                "$set": {
                    "last_error": error,
                    "next_attempt_at": now_utc() + timedelta(seconds=retry_after_seconds),
                    "updated_at": now_utc(),
                },
            },
        )
        return await self.items.find_one({"_id": item_id})

    async def defer_pending_items_until(self, *, target_key: str, when: datetime) -> None:
        """Move pending items' next_attempt_at forward to `when` (per-target).

        This is used in direct-post mode to avoid leaving pending items "due now" when
        the cadence gate (last_post_at + interval) says we shouldn't post yet.
        """
        when = _ensure_aware_utc(when) or now_utc()
        await self.items.update_many(
            {
                "target_key": target_key,
                "status": "pending",
                "next_attempt_at": {"$lt": when},
            },
            {"$set": {"next_attempt_at": when, "updated_at": now_utc()}},
        )
def _ensure_aware_utc(dt: Any) -> Optional[datetime]:
    """Best-effort normalize datetime to timezone-aware UTC."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            parsed = datetime.fromisoformat(dt)
        except Exception:
            return None
        dt = parsed
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _roll_forward(dt: datetime, *, min_dt: datetime, step_seconds: int) -> datetime:
    """Roll dt forward by N*step until dt >= min_dt (dt is returned unchanged if already >=)."""
    if dt >= min_dt:
        return dt
    if step_seconds <= 0:
        return min_dt
    delta = (min_dt - dt).total_seconds()
    steps = int(delta // step_seconds)
    if dt + timedelta(seconds=steps * step_seconds) < min_dt:
        steps += 1
    return dt + timedelta(seconds=steps * step_seconds)


def _ceil_to_minute(dt: datetime) -> datetime:
    """Ceil a datetime to the next whole minute boundary (UTC-aware expected).

    Telegram's native scheduler behaves at minute granularity; scheduling inside the
    current minute can lead to messages being scheduled "for the minute it started",
    which may already be in the past by the time Telegram processes it.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.second == 0 and dt.microsecond == 0:
        return dt
    base = dt.replace(second=0, microsecond=0)
    return base + timedelta(minutes=1)

def build_allowlist_alias_map(allowlist: List[str]) -> Dict[str, str]:
    """Return mapping of alias->canonical allowlist token.

    Canonical is the exact allowlist token. Aliases include:
    - for @name: also 'name'
    - for -100123: also '123' and '-100123'
    """
    alias_to_canonical: Dict[str, str] = {}

    def _add(alias: str, canonical: str) -> None:
        alias = alias.strip()
        if not alias:
            return
        # First wins to avoid surprises if user misconfigures
        alias_to_canonical.setdefault(alias, canonical)

    for canonical in allowlist:
        _add(canonical, canonical)

        # Username-ish aliases:
        # If user configured '@name', also accept 'name'.
        # If user configured 'name' (without @), also accept '@name'.
        if canonical.startswith("@"):  # username
            _add(canonical[1:], canonical)
        elif re.fullmatch(r"[A-Za-z0-9_]{4,}", canonical):
            # Looks like a Telegram username (very loose check). This prevents
            # common misconfig where BACKLOG_TARGETS uses 'name' but the folder
            # on disk (or logs) use '@name'.
            _add(f"@{canonical}", canonical)

        # Numeric-ish aliases
        try:
            n = int(canonical)
            s = str(n)
            _add(s, canonical)
            if s.startswith("-100"):
                _add(s.replace("-100", "", 1), canonical)
            else:
                _add(f"-100{s.lstrip('-')}", canonical)
        except Exception:
            pass

    return alias_to_canonical


# -----------------------------
# Core backlog logic
# -----------------------------

def _target_token_for_pyrogram(token: str) -> Any:
    """Return a value suitable to pass to Pyrogram methods.

    If token looks numeric, return int(token), else return token (e.g. '@name').
    """
    token = token.strip()
    if token.startswith("@"):  # username
        return token
    if re.fullmatch(r"-?\d+", token):
        try:
            return int(token)
        except Exception:
            return token
    return token

async def resolve_peer_id(app: Client, store: BacklogStore, target_key: str, allowlist_token: str) -> int:
    """Resolve and cache peer_id. allowlist_token is the configured allowlist entry."""
    target = await store.get_or_create_target(target_key)
    cached = target.get("peer_id")
    if isinstance(cached, int) and cached != 0:
        return cached

    # Try resolve from allowlist token first (and tolerate @/non-@ variants)
    try_tokens: List[str] = [allowlist_token]

    # If username-ish, try both '@name' and 'name'
    if isinstance(allowlist_token, str) and allowlist_token:
        if allowlist_token.startswith("@"):
            try_tokens.append(allowlist_token[1:])
        elif re.fullmatch(r"[A-Za-z0-9_]{4,}", allowlist_token):
            try_tokens.append(f"@{allowlist_token}")

    # If numeric without -100, also try with -100 prefix (channels/supergroups)
    if not allowlist_token.startswith("@"):
        try:
            n = int(allowlist_token)
            if not str(n).startswith("-100"):
                try_tokens.append(f"-100{str(n).lstrip('-')}".replace("--", "-"))
        except Exception:
            try_tokens.append(f"@{allowlist_token}")

    last_exc: Optional[Exception] = None
    # De-dup while preserving order
    seen: set[str] = set()
    try_tokens = [t for t in try_tokens if not (t in seen or seen.add(t))]

    for tok in try_tokens:
        try:
            chat = await app.get_chat(_target_token_for_pyrogram(tok))
            peer_id = int(chat.id)
            await store.set_target_peer_id(target_key, peer_id)
            return peer_id
        except Exception as e:
            last_exc = e

    raise RuntimeError(f"Failed to resolve peer for {target_key} via {try_tokens}: {last_exc}")


def pick_send_kind(path: Path, allow_unknown_as_document: bool) -> Optional[str]:
    ext = path.suffix.lower()
    if ext in FILETYPE_MAP:
        return FILETYPE_MAP[ext]
    if allow_unknown_as_document:
        return "document"
    return None


def is_transient_sync_file(path: Path) -> bool:
    """Return True for known temporary/in-progress sync files.

    Syncthing creates files like `.syncthing.<random>` while transferring.
    These are not user content and should never be quarantined/archived.
    """
    name = path.name
    # Syncthing temp file naming convention
    if name.startswith(".syncthing."):
        return True
    return False


def is_ignorable_metadata_file(path: Path) -> bool:
    """Return True for OS/tool metadata files we should silently ignore.

    These are not user content. We should not quarantine, archive, or enqueue them.
    """
    name = path.name
    # macOS Finder metadata
    if name == ".DS_Store" or name.startswith("._"):
        return True
    # Windows Explorer thumbnail cache
    if name.lower() == "thumbs.db":
        return True
    return False
async def quarantine_paths(cfg: BacklogConfig, *, reason: str, target_bucket: str, paths: List[Path]) -> None:
    # Move each path under archive/_quarantine/<reason>/<target_bucket>/
    base = cfg.archive_root / "_quarantine" / reason / target_bucket
    ensure_dir(base)
    for p in paths:
        if not p.exists():
            continue
        dest = base / p.name
        try:
            safe_move(p, dest)
        except Exception as e:
            logger.exception("Failed to quarantine %s to %s: %s", p, dest, e)


async def archive_paths(cfg: BacklogConfig, *, bucket: str, target_key: str, paths: List[Path]) -> None:
    base = cfg.archive_root / bucket / target_key
    ensure_dir(base)
    for p in paths:
        if not p.exists():
            continue
        dest = base / p.name
        try:
            safe_move(p, dest)
        except Exception as e:
            logger.exception("Failed to archive %s to %s: %s", p, dest, e)


async def handle_success(cfg: BacklogConfig, *, target_key: str, media: Path, sidecars: List[Path]) -> None:
    if cfg.success_action == "delete":
        safe_delete(media)
        for s in sidecars:
            safe_delete(s)
        return

    if cfg.success_action == "archive":
        await archive_paths(cfg, bucket="_posted", target_key=target_key, paths=[media] + sidecars)
        return


async def cleanup_success_item_files(cfg: BacklogConfig, item: Dict[str, Any], *, context: str) -> bool:
    """Best-effort local success action after Telegram accepted an item."""
    target_key = item["target_key"]
    media = resolve_media_path(cfg, item["rel_path"])
    sidecar = caption_sidecar_for(media)
    sidecars = [sidecar] if sidecar.exists() else []
    original_paths = [media] + sidecars
    try:
        await handle_success(cfg, target_key=target_key, media=media, sidecars=sidecars)
    except Exception:
        logger.exception(
            "%s: success-action cleanup failed (item state already persisted; "
            "file may be left in place) item_id=%s target=%s rel_path=%s action=%s",
            context,
            item.get("_id"),
            target_key,
            item["rel_path"],
            cfg.success_action,
        )
        return False

    remaining = [p for p in original_paths if p.exists()]
    if remaining:
        logger.warning(
            "%s: success-action cleanup left local files in place; will retry "
            "item_id=%s target=%s rel_path=%s action=%s remaining=%s",
            context,
            item.get("_id"),
            target_key,
            item["rel_path"],
            cfg.success_action,
            ", ".join(str(p) for p in remaining),
        )
        return False

    return True


async def mark_schedule_success(
    cfg: BacklogConfig,
    store: BacklogStore,
    item: Dict[str, Any],
    *,
    message_id: Optional[int],
    scheduled_at: datetime,
) -> None:
    target_key = item["target_key"]

    # Persist the Telegram-scheduled state before touching local files. The file
    # can be safely deleted/archived after Telegram has accepted the upload.
    await store.set_item_status(
        item["_id"],
        "scheduled",
        scheduled_message_id=message_id,
        scheduled_at=scheduled_at,
        local_success_action_at=None,
    )
    await store.targets.update_one(
        {"_id": target_key},
        {
            "$set": {
                "last_scheduled_at": scheduled_at,
                **(
                    {"cadence_next_at": scheduled_at + timedelta(seconds=cfg.interval_seconds)}
                    if cfg.scheduler_mode == "fixed_cadence"
                    else {}
                ),
                "updated_at": now_utc(),
            }
        },
    )

    if await cleanup_success_item_files(cfg, item, context="mark_schedule_success"):
        await store.mark_local_success_action_applied(item["_id"])


async def cleanup_scheduled_local_files(
    cfg: BacklogConfig,
    store: BacklogStore,
    *,
    target_key: Optional[str] = None,
) -> None:
    query: Dict[str, Any] = {
        "status": "scheduled",
        "$or": [
            {"local_success_action_at": {"$exists": False}},
            {"local_success_action_at": None},
        ],
    }
    if target_key is not None:
        query["target_key"] = target_key

    async for item in store.items.find(query):
        try:
            if await cleanup_success_item_files(cfg, item, context="cleanup_scheduled_local_files"):
                await store.mark_local_success_action_applied(item["_id"])
                logger.info(
                    "cleanup_scheduled_local_files: applied success action target=%s rel=%s",
                    item.get("target_key"),
                    item.get("rel_path"),
                )
        except Exception:
            logger.exception(
                "cleanup_scheduled_local_files: unhandled cleanup error target=%s rel=%s item_id=%s",
                item.get("target_key"),
                item.get("rel_path"),
                item.get("_id"),
            )


def is_leftover_of_posted_item(existing: Dict[str, Any], current_rel_path: str) -> bool:
    """True if a rediscovered file is the same already-posted item (cleanup failed
    earlier), as opposed to a genuinely separate file with identical content."""
    return existing.get("rel_path") == current_rel_path


def rel_path_from_any_root(cfg: BacklogConfig, path: Path) -> str:
    """Return a rel_path for a file under any configured backlog root."""
    for root in cfg.backlog_roots:
        try:
            return str(path.relative_to(root))
        except ValueError:
            continue
    # Backwards-compatible fallback for older single-root assumptions.
    return str(path.relative_to(cfg.backlog_root))


def resolve_media_path(cfg: BacklogConfig, rel_path: str) -> Path:
    """Resolve a stored rel_path to the first matching file across backlog roots."""
    for root in cfg.backlog_roots:
        candidate = root / rel_path
        if candidate.exists():
            return candidate
    return cfg.backlog_root / rel_path


async def scan_backlog(cfg: BacklogConfig, store: BacklogStore, app: Optional[Client] = None) -> None:
    """Scan backlog roots, discover stable media files, and enqueue them in DB.

    If `app` is provided, we can be "smarter" about mapping folder names to the
    configured allowlist by resolving Telegram peers (e.g. allowlist contains a
    numeric chat id but the folder on disk is an @username).
    """
    logger.debug("scan_backlog: starting scan")
    for r in cfg.backlog_roots:
        ensure_dir(r)
    allowlist = parse_allowlist(cfg.targets_allowlist)

    alias_map = build_allowlist_alias_map(allowlist)
    # Per-scan cache so we don't repeatedly resolve the same folder name.
    resolved_folder_to_canonical: Dict[str, str] = {}

    logger.info(
        "scan_backlog: roots=%s allowlist_count=%d allowlist=%s",
        cfg.backlog_roots,
        len(allowlist),
        allowlist,
    )

    for root in cfg.backlog_roots:
        try:
            children = list(root.iterdir())
        except Exception:
            logger.exception("scan_backlog: failed to list backlog root %s", root)
            continue

        logger.info("scan_backlog: root=%s found %d entries under root", root, len(children))

        for child in children:
            if not child.is_dir():
                logger.debug("scan_backlog: skipping non-dir %s", child)
                continue

            folder_name = child.name

            # We only scan folders that can map to allowlist somehow.
            # Mapping strategy:
            #   - Fast path: alias_map (format variants like @name<->name, -100id<->id)
            #   - Smart path (if app provided): resolve folder peer_id and match it to
            #     one of the allowlisted tokens by peer_id.
            #
            # Folder names we consider "target-like":
            #   - @username
            #   - numeric (chat id)
            #   - bare username-ish (so folders named 'MyChannel' work too)
            looks_like_target = (
                folder_name.startswith("@")
                or re.fullmatch(r"-?\d+", folder_name) is not None
                or re.fullmatch(r"[A-Za-z0-9_]{4,}", folder_name) is not None
            )

            if not looks_like_target:
                if cfg.skip_quarantine_unmapped_targets:
                    logger.info(
                        "scan_backlog: root=%s folder=%s not target-like; skipping quarantine (config)",
                        root,
                        folder_name,
                    )
                else:
                    logger.info(
                        "scan_backlog: root=%s folder=%s not target-like; quarantining files (if any)",
                        root,
                        folder_name,
                    )
                    # Not a target-like folder; quarantine its contents.
                    files = [p for p in child.iterdir() if p.is_file() and not is_ignorable_metadata_file(p)]
                    if files:
                        await quarantine_paths(cfg, reason="unmapped_target", target_bucket=folder_name, paths=files)
                continue

            canonical_target = resolved_folder_to_canonical.get(folder_name) or alias_map.get(folder_name)
            if canonical_target is None and app is not None:
                # "Smart" mapping: allow allowlist entries like -100123... while
                # accepting folders like @MyChannel (or MyChannel) by resolving peer_id.
                try:
                    folder_peer_id = await resolve_peer_id(
                        app,
                        store,
                        target_key=folder_name,
                        allowlist_token=folder_name,
                    )
                except Exception:
                    folder_peer_id = None

                if isinstance(folder_peer_id, int):
                    for allow_tok in allowlist:
                        try:
                            allow_peer_id = await resolve_peer_id(
                                app,
                                store,
                                target_key=allow_tok,
                                allowlist_token=allow_tok,
                            )
                        except Exception:
                            continue
                        if allow_peer_id == folder_peer_id:
                            canonical_target = allow_tok
                            # Cache for the rest of this scan cycle.
                            resolved_folder_to_canonical[folder_name] = canonical_target
                            break
            if canonical_target is None:
                if cfg.skip_quarantine_unmapped_targets:
                    logger.info(
                        "scan_backlog: root=%s folder=%s not allowlisted/mappable; skipping quarantine (config)",
                        root,
                        folder_name,
                    )
                else:
                    logger.info(
                        "scan_backlog: root=%s folder=%s not allowlisted/mappable; quarantining files (if any)",
                        root,
                        folder_name,
                    )
                    # Folder exists but not allowlisted/mappable: quarantine contents.
                    files = [p for p in child.iterdir() if p.is_file() and not is_ignorable_metadata_file(p)]
                    if files:
                        await quarantine_paths(cfg, reason="unmapped_target", target_bucket=folder_name, paths=files)
                continue

            target_key = canonical_target
            await store.get_or_create_target(target_key)

            try:
                folder_files = list(child.iterdir())
            except Exception:
                logger.exception("scan_backlog: failed to list folder %s", child)
                continue

            logger.info(
                "scan_backlog: root=%s scanning target folder=%s (canonical=%s) entries=%d",
                root,
                child,
                target_key,
                len(folder_files),
            )

            for p in folder_files:
                if not p.is_file():
                    continue

                # Ignore OS/tool metadata files entirely
                if is_ignorable_metadata_file(p):
                    logger.debug("scan_backlog: ignoring metadata file: %s", p)
                    continue

                # Ignore transient sync/in-progress temp files (e.g. Syncthing)
                if is_transient_sync_file(p):
                    logger.debug("scan_backlog: ignoring transient sync file: %s", p)
                    continue

                # Ignore sidecars themselves; we only enqueue the media.
                if p.name.endswith(".caption.txt"):
                    continue

                if not is_stable_file(p, cfg.settle_seconds):
                    logger.debug("scan_backlog: file not settled yet: %s", p)
                    continue

                # De-dup by content hash per target.
                # IMPORTANT: only quarantine when we know the content was already POSTED.
                # If the existing item is merely pending/scheduled, it's not a true duplicate
                # yet (it might never get posted), so we leave this file alone.
                try:
                    sha_for_dedup = compute_sha256(p)
                    existing = await store.find_existing_content_item(target_key=target_key, sha256=sha_for_dedup)
                except Exception as e:
                    logger.warning("scan_backlog: failed duplicate check for %s: %s", p, e)
                    existing = None
                    sha_for_dedup = None

                if existing and existing.get("status") == "posted":
                    current_rel = rel_path_from_any_root(cfg, p)
                    if is_leftover_of_posted_item(existing, current_rel):
                        # Same file, same path, already marked posted: this is a
                        # leftover from a post-success cleanup failure, not a true
                        # duplicate. Retry cleanup instead of quarantining it.
                        logger.warning(
                            "scan_backlog: leftover posted file still on disk; retrying cleanup target=%s rel=%s sha=%s",
                            target_key,
                            current_rel,
                            (sha_for_dedup or "")[:12],
                        )
                        sidecar = caption_sidecar_for(p)
                        sidecars = [sidecar] if sidecar.exists() else []
                        try:
                            await handle_success(cfg, target_key=target_key, media=p, sidecars=sidecars)
                        except Exception:
                            logger.exception(
                                "scan_backlog: retry cleanup failed for leftover posted file target=%s rel=%s",
                                target_key,
                                current_rel,
                            )
                        continue

                    logger.info(
                        "scan_backlog: duplicate content; quarantining target=%s rel=%s sha=%s existing_status=%s existing_rel=%s",
                        target_key,
                        current_rel,
                        (sha_for_dedup or "")[:12],
                        existing.get("status"),
                        existing.get("rel_path"),
                    )
                    sidecar = caption_sidecar_for(p)
                    to_quarantine = [p] + ([sidecar] if sidecar.exists() else [])
                    await quarantine_paths(
                        cfg,
                        reason="duplicate",
                        target_bucket=target_key,
                        paths=to_quarantine,
                    )
                    continue
                elif existing:
                    logger.debug(
                        "scan_backlog: duplicate content but existing_status=%s (not quarantining) target=%s rel=%s sha=%s existing_rel=%s",
                        existing.get("status"),
                        target_key,
                        rel_path_from_any_root(cfg, p),
                        (sha_for_dedup or "")[:12],
                        existing.get("rel_path"),
                    )

                send_kind = pick_send_kind(p, cfg.allow_unknown_as_document)
                if send_kind is None:
                    logger.info("scan_backlog: disallowed filetype; quarantining: %s", p)
                    sidecar = caption_sidecar_for(p)
                    to_quarantine = [p] + ([sidecar] if sidecar.exists() else [])
                    await quarantine_paths(
                        cfg,
                        reason="disallowed_unknown",
                        target_bucket=target_key,
                        paths=to_quarantine,
                    )
                    continue

                try:
                    st = p.stat()
                    # Reuse the earlier hash if we already computed it for dedupe.
                    sha = sha_for_dedup or compute_sha256(p)
                    rel_path = rel_path_from_any_root(cfg, p)
                    inserted = await store.upsert_item_discovered(
                        target_key=target_key,
                        rel_path=rel_path,
                        sha256=sha,
                        size=int(st.st_size),
                        mtime=float(st.st_mtime),
                        send_kind=send_kind,
                    )
                    if inserted:
                        logger.info(
                            "scan_backlog: enqueued target=%s rel=%s kind=%s size=%d sha=%s",
                            target_key,
                            rel_path,
                            send_kind,
                            int(st.st_size),
                            sha[:12],
                        )
                    else:
                        logger.debug("scan_backlog: already enqueued (skip) target=%s rel=%s", target_key, rel_path)
                except Exception as e:
                    logger.exception("Failed to enqueue %s: %s", p, e)


async def send_one_item(
    cfg: BacklogConfig,
    store: BacklogStore,
    app: Client,
    item: Dict[str, Any],
    peer_id: int,
    *,
    schedule_date: Optional[datetime] = None,
    apply_success_action: bool = True,
) -> Tuple[bool, Optional[int], Optional[str]]:
    """Send or schedule one item. Returns (success, message_id, error)."""
    rel_path = item["rel_path"]
    media_path = resolve_media_path(cfg, rel_path)
    target_key = item["target_key"]

    if not media_path.exists():
        return False, None, f"missing file: {media_path}"

    sidecar = caption_sidecar_for(media_path)
    caption = None
    if sidecar.exists():
        try:
            caption = sidecar.read_text(encoding="utf-8").strip()
        except Exception:
            caption = None

    # In scheduler mode, append marker to caption if caption exists (for dedup/reconciliation).
    # Do not add a caption if the user did not provide one.
    if cfg.use_telegram_scheduler:
        # Note: stickers cannot carry captions; for those we rely on message_id reconciliation.
        if item.get("send_kind") != "sticker":
            marker = sha_marker(item["sha256"])
            if caption:
                caption = caption + marker
            # else: leave caption as None

    send_kind = item["send_kind"]

    try:
        if send_kind == "photo":
            msg = await app.send_photo(
                peer_id,
                photo=str(media_path),
                caption=caption,
                schedule_date=schedule_date,
            )
        elif send_kind == "video":
            msg = await app.send_video(
                peer_id,
                video=str(media_path),
                caption=caption,
                schedule_date=schedule_date,
            )
        elif send_kind == "animation":
            msg = await app.send_animation(
                peer_id,
                animation=str(media_path),
                caption=caption,
                schedule_date=schedule_date,
            )
        elif send_kind == "sticker":
            # Stickers do not support captions; marker cannot be embedded.
            msg = await app.send_sticker(
                peer_id,
                sticker=str(media_path),
                schedule_date=schedule_date,
            )
        elif send_kind == "document":
            msg = await app.send_document(
                peer_id,
                document=str(media_path),
                caption=caption,
                schedule_date=schedule_date,
            )
        else:
            return False, None, f"unsupported send_kind: {send_kind}"

        message_id = int(getattr(msg, "id", 0)) if msg else None

        if apply_success_action:
            sidecars = [sidecar] if sidecar.exists() else []
            await handle_success(cfg, target_key=target_key, media=media_path, sidecars=sidecars)
        return True, message_id, None

    except FloodWait as e:
        await asyncio.sleep(_floodwait_seconds(e))
        return False, None, f"FloodWait: {e}"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


async def should_post_now(cfg: BacklogConfig, target_doc: Dict[str, Any]) -> bool:
    return now_utc() >= next_post_due_at(cfg, target_doc)


def next_post_due_at(cfg: BacklogConfig, target_doc: Dict[str, Any]) -> datetime:
    """Return the next time we're allowed to post for this target in direct mode."""
    last_dt = as_aware_utc(target_doc.get("last_post_at"))
    if last_dt is None:
        return now_utc()

    return last_dt + timedelta(seconds=cfg.interval_seconds)


async def mark_post_success(
    cfg: BacklogConfig,
    store: BacklogStore,
    item: Dict[str, Any],
    *,
    message_id: Optional[int],
    posted_at: Optional[datetime] = None,
) -> None:
    target_key = item["target_key"]
    posted_at = posted_at or now_utc()

    # Persist the irreversible fact FIRST: a Telegram message already exists for
    # this item. Once this write lands, the item can never be re-enqueued/reposted,
    # regardless of what happens to local file cleanup below.
    await store.set_item_status(
        item["_id"],
        "posted",
        posted_message_id=message_id,
        posted_at=posted_at,
        scheduled_message_id=None,
    )
    await store.targets.update_one(
        {"_id": target_key},
        {"$set": {"last_post_at": posted_at, "updated_at": now_utc()}},
    )

    # Best-effort local cleanup. Must never undo the guarantee above or crash the
    # caller: log loudly on failure and move on.
    await cleanup_success_item_files(cfg, item, context="mark_post_success")


async def direct_post_loop(cfg: BacklogConfig, store: BacklogStore, app: Client) -> None:
    allowlist = parse_allowlist(cfg.targets_allowlist)
    alias_map = build_allowlist_alias_map(allowlist)

    # Optionally allow a one-time "kick" at startup: post one item per target immediately,
    # even if last_post_at cadence says we should wait. This helps after restarts.
    started_at = now_utc()
    posted_immediately_for: set[str] = set()

    while True:
        if not cfg.enabled:
            await asyncio.sleep(5)
            continue

        logger.debug("direct_post_loop: tick scope=%s", cfg.scope)
        await scan_backlog(cfg, store, app)

        if cfg.scope == "global":
            item = await store.get_next_due_item(target_key=None)
            if not item:
                logger.debug("direct_post_loop: no due items (global)")
                await asyncio.sleep(cfg.scan_every_seconds)
                continue

            target_key = item["target_key"]
            if target_key not in alias_map.values():
                # should not happen if scanning obeys allowlist, but safe
                await store.set_item_status(item["_id"], "quarantined", last_error="target not allowlisted")
                await asyncio.sleep(1)
                continue

            target_doc = await store.get_or_create_target(target_key)
            if not await should_post_now(cfg, target_doc):
                # Startup kick (global scope): allow one immediate post per target even if cadence says wait.
                if (
                    cfg.immediate_post_on_start
                    and target_key not in posted_immediately_for
                    and (now_utc() - started_at).total_seconds() <= max(30, cfg.scan_every_seconds * 2)
                ):
                    logger.info(
                        "direct_post_loop: immediate startup post (global) for target=%s rel=%s",
                        target_key,
                        item.get("rel_path"),
                    )
                else:
                    logger.debug("direct_post_loop: should_post_now=false for target=%s", target_key)
                    # Defer pending items for this target until they're actually due to avoid starvation
                    due_at = next_post_due_at(cfg, target_doc)
                    try:
                        await store.defer_pending_items_until(target_key=target_key, when=due_at)
                    except Exception:
                        logger.exception("direct_post_loop: failed deferring pending items for target=%s", target_key)
                    await asyncio.sleep(5)
                    continue

            try:
                peer_id = await resolve_peer_id(app, store, target_key, target_key)
                ok, msg_id, err = await send_one_item(
                    cfg,
                    store,
                    app,
                    item,
                    peer_id,
                    apply_success_action=False,
                )
                if cfg.immediate_post_on_start and target_key not in posted_immediately_for:
                    posted_immediately_for.add(target_key)
                if ok:
                    logger.info(
                        "direct_post_loop: posted target=%s rel=%s msg_id=%s",
                        target_key,
                        item.get("rel_path"),
                        msg_id,
                    )
                    await mark_post_success(cfg, store, item, message_id=msg_id)
                else:
                    logger.warning(
                        "direct_post_loop: send failed target=%s rel=%s err=%s",
                        target_key,
                        item.get("rel_path"),
                        err,
                    )
                    updated = await store.bump_failure(item["_id"], err or "unknown", retry_after_seconds=cfg.scan_every_seconds)
                    failure_limit = MISSING_FILE_MAX_RETRIES if err and err.startswith("missing file:") else cfg.max_failures
                    if updated and int(updated.get("fail_count", 0)) >= failure_limit:
                        # move file to failed archive bucket
                        rel = updated["rel_path"]
                        media = resolve_media_path(cfg, rel)
                        sidecar = caption_sidecar_for(media)
                        paths = [media] + ([sidecar] if sidecar.exists() else [])
                        await archive_paths(cfg, bucket="_failed", target_key=target_key, paths=paths)
                        await store.set_item_status(item["_id"], "failed")
            except Exception:
                logger.exception(
                    "direct_post_loop: unhandled error processing item (global) target=%s rel=%s item_id=%s",
                    target_key,
                    item.get("rel_path"),
                    item.get("_id"),
                )
            await asyncio.sleep(1)
            continue

        # per-target scope
        for target_key in allowlist:
            target_doc = await store.get_or_create_target(target_key)
            if not await should_post_now(cfg, target_doc):
                # Startup kick: allow one immediate post per target if there is something queued.
                if (
                    cfg.immediate_post_on_start
                    and target_key not in posted_immediately_for
                    and (now_utc() - started_at).total_seconds() <= max(30, cfg.scan_every_seconds * 2)
                ):
                    item = await store.get_next_due_item(target_key=target_key)
                    if item:
                        logger.info(
                            "direct_post_loop: immediate startup post for target=%s rel=%s",
                            target_key,
                            item.get("rel_path"),
                        )
                        try:
                            peer_id = await resolve_peer_id(app, store, target_key, target_key)
                            ok, msg_id, err = await send_one_item(
                                cfg,
                                store,
                                app,
                                item,
                                peer_id,
                                apply_success_action=False,
                            )
                            posted_immediately_for.add(target_key)
                            if ok:
                                logger.info(
                                    "direct_post_loop: posted (startup) target=%s rel=%s msg_id=%s",
                                    target_key,
                                    item.get("rel_path"),
                                    msg_id,
                                )
                                await mark_post_success(cfg, store, item, message_id=msg_id)
                            else:
                                logger.warning(
                                    "direct_post_loop: send failed (startup) target=%s rel=%s err=%s",
                                    target_key,
                                    item.get("rel_path"),
                                    err,
                                )
                                updated = await store.bump_failure(
                                    item["_id"],
                                    err or "unknown",
                                    retry_after_seconds=cfg.scan_every_seconds,
                                )
                                failure_limit = MISSING_FILE_MAX_RETRIES if err and err.startswith("missing file:") else cfg.max_failures
                                if updated and int(updated.get("fail_count", 0)) >= failure_limit:
                                    rel = updated["rel_path"]
                                    media = resolve_media_path(cfg, rel)
                                    sidecar = caption_sidecar_for(media)
                                    paths = [media] + ([sidecar] if sidecar.exists() else [])
                                    await archive_paths(cfg, bucket="_failed", target_key=target_key, paths=paths)
                                    await store.set_item_status(item["_id"], "failed")
                        except Exception:
                            logger.exception(
                                "direct_post_loop: unhandled error processing item (startup) target=%s rel=%s item_id=%s",
                                target_key,
                                item.get("rel_path"),
                                item.get("_id"),
                            )
                            posted_immediately_for.add(target_key)
                        # Continue to next target; do not also run defer logic this tick.
                        continue

                due_at = next_post_due_at(cfg, target_doc)
                # Keep DB semantics accurate: items shouldn't appear "due now" when we're
                # intentionally rate-limiting per target.
                try:
                    await store.defer_pending_items_until(target_key=target_key, when=due_at)
                except Exception:
                    logger.exception("direct_post_loop: failed deferring pending items for target=%s", target_key)

                logger.debug(
                    "direct_post_loop: skip target=%s (not time yet; next_due=%s)",
                    target_key,
                    due_at,
                )
                continue

            item = await store.get_next_due_item(target_key=target_key)
            if not item:
                logger.debug("direct_post_loop: no due item for target=%s", target_key)
                continue

            try:
                peer_id = await resolve_peer_id(app, store, target_key, target_key)
                ok, msg_id, err = await send_one_item(
                    cfg,
                    store,
                    app,
                    item,
                    peer_id,
                    apply_success_action=False,
                )
                if ok:
                    logger.info(
                        "direct_post_loop: posted target=%s rel=%s msg_id=%s",
                        target_key,
                        item.get("rel_path"),
                        msg_id,
                    )
                    await mark_post_success(cfg, store, item, message_id=msg_id)
                else:
                    logger.warning(
                        "direct_post_loop: send failed target=%s rel=%s err=%s",
                        target_key,
                        item.get("rel_path"),
                        err,
                    )
                    updated = await store.bump_failure(item["_id"], err or "unknown", retry_after_seconds=cfg.scan_every_seconds)
                    failure_limit = MISSING_FILE_MAX_RETRIES if err and err.startswith("missing file:") else cfg.max_failures
                    if updated and int(updated.get("fail_count", 0)) >= failure_limit:
                        rel = updated["rel_path"]
                        media = resolve_media_path(cfg, rel)
                        sidecar = caption_sidecar_for(media)
                        paths = [media] + ([sidecar] if sidecar.exists() else [])
                        await archive_paths(cfg, bucket="_failed", target_key=target_key, paths=paths)
                        await store.set_item_status(item["_id"], "failed")
            except Exception:
                logger.exception(
                    "direct_post_loop: unhandled error processing item target=%s rel=%s item_id=%s",
                    target_key,
                    item.get("rel_path"),
                    item.get("_id"),
                )

        await asyncio.sleep(cfg.scan_every_seconds)


async def scheduler_reconcile(cfg: BacklogConfig, app: Client, store: BacklogStore, target_key: str) -> None:
    """Fetch scheduled messages and reconcile DB. Best-effort."""
    # Pyrogram API varies; guard to avoid crashes.
    getter = getattr(app, "get_scheduled_messages", None)
    if getter is None:
        logger.warning("Pyrogram client has no get_scheduled_messages; skipping reconciliation")
        async for item in store.items.find({"target_key": target_key, "status": "scheduled"}):
            try:
                scheduled_at = as_aware_utc(item.get("scheduled_at"))
                if scheduled_at is not None and scheduled_at <= now_utc():
                    await mark_post_success(
                        cfg,
                        store,
                        item,
                        message_id=item.get("scheduled_message_id"),
                        posted_at=scheduled_at,
                    )
            except Exception:
                logger.exception(
                    "scheduler_reconcile: unhandled error reconciling (fallback) target=%s rel=%s item_id=%s",
                    target_key,
                    item.get("rel_path"),
                    item.get("_id"),
                )
        return

    try:
        scheduled = await getter(_target_token_for_pyrogram(target_key))
    except Exception as e:
        logger.warning("Failed to fetch scheduled messages for %s: %s", target_key, e)
        return

    # Build set of message ids that exist
    existing_ids = set()
    for m in scheduled or []:
        mid = getattr(m, "id", None)
        if mid is not None:
            existing_ids.add(int(mid))

    # If a future scheduled message disappeared, assume it was cancelled and requeue it.
    # If the scheduled time has passed and Telegram no longer lists it, treat it as posted.
    async for item in store.items.find({"target_key": target_key, "status": "scheduled"}):
        mid = item.get("scheduled_message_id")
        if isinstance(mid, int) and mid in existing_ids:
            continue

        try:
            scheduled_at = as_aware_utc(item.get("scheduled_at"))
            if scheduled_at is not None and scheduled_at <= now_utc():
                await mark_post_success(cfg, store, item, message_id=mid, posted_at=scheduled_at)
                logger.info(
                    "scheduler_reconcile: marked posted target=%s rel=%s scheduled_at=%s",
                    target_key,
                    item.get("rel_path"),
                    scheduled_at,
                )
                continue

            await store.set_item_status(
                item["_id"],
                "pending",
                scheduled_message_id=None,
                scheduled_at=None,
                next_attempt_at=now_utc(),
            )
        except Exception:
            logger.exception(
                "scheduler_reconcile: unhandled error reconciling target=%s rel=%s item_id=%s",
                target_key,
                item.get("rel_path"),
                item.get("_id"),
            )


async def telegram_scheduler_loop(cfg: BacklogConfig, store: BacklogStore, app: Client) -> None:
    allowlist = parse_allowlist(cfg.targets_allowlist)

    while True:
        if not cfg.enabled:
            await asyncio.sleep(5)
            continue

        await scan_backlog(cfg, store, app)

        horizon = now_utc() + timedelta(seconds=cfg.schedule_ahead_seconds)

        for target_key in allowlist:
            # reconcile scheduled queue in TG
            await scheduler_reconcile(cfg, app, store, target_key)

            target_doc = await store.get_or_create_target(target_key)
            peer_id = await resolve_peer_id(app, store, target_key, target_key)

            # Telegram scheduling is effectively minute-granular; avoid scheduling within the
            # current minute on startup by rounding up to the next minute boundary.
            min_next = _ceil_to_minute(now_utc() + timedelta(seconds=cfg.min_schedule_delay_seconds))

            if cfg.scheduler_mode == "fixed_cadence":
                # cadence_next_at is the persisted "next slot". It survives restarts.
                # On each loop we ensure it's at least now+min_delay, rolling forward by interval.
                cadence_next = _ensure_aware_utc(target_doc.get("cadence_next_at"))
                if cadence_next is None:
                    # Back-compat: seed from last_scheduled_at if present, otherwise from min_next.
                    last_sched_dt = _ensure_aware_utc(target_doc.get("last_scheduled_at"))
                    if last_sched_dt is not None:
                        cadence_next = max(min_next, last_sched_dt + timedelta(seconds=cfg.interval_seconds))
                    else:
                        cadence_next = min_next
                    try:
                        await store.targets.update_one(
                            {"_id": target_key},
                            {"$set": {"cadence_next_at": cadence_next, "updated_at": now_utc()}},
                        )
                    except Exception:
                        logger.exception("scheduler: failed to initialize cadence_next_at for %s", target_key)

                if cadence_next < min_next and cfg.overdue == "post_once":
                    next_time = min_next
                else:
                    next_time = _ceil_to_minute(
                        _roll_forward(cadence_next, min_dt=min_next, step_seconds=cfg.interval_seconds)
                    )
            else:
                # Legacy behavior: schedule based on last_scheduled_at, but clamp into the future.
                last_sched_dt = as_aware_utc(target_doc.get("last_scheduled_at"))
                candidate = (last_sched_dt + timedelta(seconds=cfg.interval_seconds)) if last_sched_dt else None
                if candidate is None:
                    next_time = min_next
                elif candidate >= min_next:
                    next_time = _ceil_to_minute(candidate)
                elif cfg.overdue == "wait":
                    next_time = _ceil_to_minute(
                        _roll_forward(candidate, min_dt=min_next, step_seconds=cfg.interval_seconds)
                    )
                else:
                    next_time = min_next

            # Schedule items until horizon. This is the point of scheduler mode: fill
            # Telegram's scheduled-message queue with one backlog item per interval.
            # If we only schedule one item here, a restart/redeploy can leave just the
            # first post queued and make the configured interval appear broken.
            while next_time <= horizon:
                item = await store.get_next_due_item(target_key=target_key)
                if not item:
                    break

                ok, msg_id, err = await send_one_item(
                    cfg,
                    store,
                    app,
                    item,
                    peer_id,
                    schedule_date=next_time,
                    apply_success_action=False,
                )

                if ok:
                    logger.info(
                        "scheduler: scheduled target=%s rel=%s at=%s msg_id=%s",
                        target_key,
                        item.get("rel_path"),
                        next_time,
                        msg_id,
                    )
                    await mark_schedule_success(
                        cfg,
                        store,
                        item,
                        message_id=msg_id,
                        scheduled_at=next_time,
                    )
                    # increment next schedule slot
                    next_time = _ceil_to_minute(next_time + timedelta(seconds=cfg.interval_seconds))
                else:
                    logger.warning(
                        "scheduler: schedule failed target=%s rel=%s at=%s err=%s",
                        target_key,
                        item.get("rel_path"),
                        next_time,
                        err,
                    )
                    updated = await store.bump_failure(item["_id"], err or "unknown", retry_after_seconds=cfg.scan_every_seconds)
                    if updated and int(updated.get("fail_count", 0)) >= cfg.max_failures:
                        rel = updated["rel_path"]
                        media = resolve_media_path(cfg, rel)
                        sidecar = caption_sidecar_for(media)
                        paths = [media] + ([sidecar] if sidecar.exists() else [])
                        await archive_paths(cfg, bucket="_failed", target_key=target_key, paths=paths)
                        await store.set_item_status(item["_id"], "failed")
                    break

        await cleanup_scheduled_local_files(cfg, store)
        await asyncio.sleep(cfg.scan_every_seconds)


async def main() -> None:
    configure_logging()

    _log_env_snapshot()

    cfg = load_config()
    logger.info(
        "Starting backlogbot: enabled=%s scheduler=%s root=%s archive=%s allowlist=%d interval=%ss",
        cfg.enabled,
        cfg.use_telegram_scheduler,
        cfg.backlog_root,
        cfg.archive_root,
        len(cfg.targets_allowlist),
        cfg.interval_seconds,
    )

    # Mongo
    logger.info("Connecting to mongo (db=%s) ...", cfg.backlog_state_db)
    conn = AsyncIOMotorClient(cfg.mongo_url)
    store = BacklogStore(conn, cfg.backlog_state_db)
    try:
        await store.ensure_indexes()
        logger.info("Mongo indexes ensured")
    except Exception:
        logger.exception("Failed ensuring Mongo indexes")

    api_id = os.getenv("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH")
    password = os.getenv("TG_PASSWORD", None)

    if not api_id or not api_hash:
        raise RuntimeError("Missing TG_API_ID/TG_API_HASH env vars")

    app_name = os.getenv("FLY_APP_NAME", "Docker")
    commit_hash = os.getenv("COMMIT_HASH", "unknown")

    app = Client(
        "backlogbot",
        api_id=api_id,
        api_hash=api_hash,
        lang_code="en",
        app_version=f"| {app_name} {commit_hash}",
        device_model="BacklogBot",
        client_platform="Linux",
        in_memory=False,
        no_updates=False,
        skip_updates=False,
        use_qrcode=True,
        mongodb=dict(connection=conn, remove_peers=False),
        password=f"{password}" if password else None,
    )

    async with app:
        logger.info("Pyrogram client started")
        if not cfg.targets_allowlist:
            logger.warning("BACKLOG_TARGETS is empty; nothing will be posted")

        if cfg.use_telegram_scheduler:
            await telegram_scheduler_loop(cfg, store, app)
        else:
            await direct_post_loop(cfg, store, app)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
