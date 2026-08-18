import os
import sys
import io
import re
import json
import time
import uuid
import glob
import sqlite3
import secrets
import requests
import libsql_client
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory
from google import genai
from google.genai import types
from gtts import gTTS
from pydub import AudioSegment  # pip install pydub --break-system-packages
import fitz  # PyMuPDF — pip install pymupdf --break-system-packages
import io
                                 # also requires ffmpeg installed on the system
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import threading
from collections import defaultdict, deque
from functools import wraps

# ===== LOAD ENV =====
load_dotenv()

# ===== UTF-8 FIX (SAFE) =====
try:
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    if hasattr(sys.stderr, 'buffer'):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
except Exception as e:
    print(f"⚠️ Could not rewrap stdout/stderr as UTF-8: {e}")

app = Flask(__name__)

# Needed for Flask's session cookies (used by the admin dashboard
# login). Set ADMIN_SECRET_KEY in Render's environment variables for
# a stable value — falling back to a random one is fine for local
# testing, but would log everyone out of /admin on every restart in
# production.
app.secret_key = os.environ.get('ADMIN_SECRET_KEY', os.urandom(24).hex())
if not os.environ.get('ADMIN_SECRET_KEY'):
    print(
        "⚠️ ADMIN_SECRET_KEY not set — using a RANDOM key generated on every restart. "
        "This logs EVERYONE out whenever the server restarts/redeploys/spins down. "
        "Set ADMIN_SECRET_KEY to a fixed random string in Render's environment variables "
        "to keep people logged in across restarts."
    )
# FIX (users were getting logged out every time they reopened the app):
# by default, Flask session cookies are "session cookies" that expire
# the moment the browser/app is closed — NOT persisted across app
# restarts on the person's own device. Marking the session permanent
# (with a real, long lifetime) makes the login cookie itself survive
# closing and reopening the app, as long as ADMIN_SECRET_KEY above is
# also a STABLE value (see the warning above — a random key that
# changes on every server restart would still invalidate all sessions
# regardless of this cookie lifetime setting).
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

# Simple password gate for the /admin usage dashboard. Set
# ADMIN_PASSWORD in Render's environment variables — do NOT hardcode
# a real password here in source control.
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB

# ========================================
# SENTRY ERROR TRACKING (optional — only activates if SENTRY_DSN is set)
# ========================================
# Free Sentry account: sentry.io → create a Flask project → copy the
# DSN → set it as SENTRY_DSN in Render's environment variables. Once
# set, unhandled errors show up in Sentry's dashboard (with full stack
# traces) instead of only being visible by scrolling through Render logs.
SENTRY_DSN = os.environ.get('SENTRY_DSN')
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[FlaskIntegration()],
            traces_sample_rate=0.1,  # light performance sampling, not just errors
            send_default_pii=False,  # don't send request bodies/user data — notes text stays private
        )
        print("✅ Sentry error tracking configured!")
    except Exception as e:
        print(f"⚠️ Sentry setup failed (non-critical): {e}")
else:
    print("⚠️ SENTRY_DSN not set — error tracking disabled (this is fine, just optional).")

# ========================================
# GOOGLE SIGN-IN (OAuth 2.0)
# ========================================
# Requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to be set in
# Render's environment variables (from Google Cloud Console → APIs &
# Services → Credentials). GOOGLE_REDIRECT_URI defaults to the
# production callback URL but can be overridden for local testing.
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')
GOOGLE_REDIRECT_URI = os.environ.get('GOOGLE_REDIRECT_URI', 'https://notewav.onrender.com/auth/google/callback')
GOOGLE_AUTH_ENDPOINT = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_ENDPOINT = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_ENDPOINT = 'https://www.googleapis.com/oauth2/v3/userinfo'
GOOGLE_LOGIN_CONFIGURED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
print("✅ Google Sign-In is configured!" if GOOGLE_LOGIN_CONFIGURED else "⚠️ Google Sign-In not configured (missing GOOGLE_CLIENT_ID/SECRET)")

# ========================================
# NOTES LIBRARY (SQLite — save/organize notes by subject)
# ========================================
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notewav.db')

# ========================================
# TURSO (persistent hosted SQLite) — replaces the local notewav.db file
# ========================================
# WHY: Render's free tier filesystem is ephemeral — the local
# notewav.db file (and everything in it: notes, users, usage_events)
# is wiped on every redeploy/restart/spin-down. Turso is a genuinely
# persistent, SQLite-compatible hosted database with a free tier that
# never expires — pointing the app at it instead means data survives
# deploys with essentially no code changes elsewhere in this file.
#
# HOW: a small compatibility layer below (TursoRow / TursoCursorResult
# / TursoConnection) mimics the exact subset of sqlite3's own API this
# file already uses (.execute(sql, params), .fetchall(), .fetchone(),
# .commit(), .close(), and dict(row)/row['col'] access on results) —
# so get_db() is the ONLY thing that changes; every other conn.execute
# (...) call throughout this file keeps working completely unchanged.
#
# SAFETY NET: if TURSO_DATABASE_URL / TURSO_AUTH_TOKEN aren't set
# (e.g. running locally without them configured yet), this falls back
# to the original local sqlite3 file — nothing breaks, it just won't
# persist across deploys until those two env vars are added.
TURSO_DATABASE_URL = os.environ.get('TURSO_DATABASE_URL')
TURSO_AUTH_TOKEN = os.environ.get('TURSO_AUTH_TOKEN')
USE_TURSO = bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)

if USE_TURSO:
    print("✅ Turso persistent database configured!")
else:
    print("⚠️ TURSO_DATABASE_URL/TURSO_AUTH_TOKEN not set — using local SQLite (data will NOT persist across deploys).")


class TursoRow:
    """Mimics sqlite3.Row: supports row['col_name'], row[0], dict(row),
    and iteration — the exact behaviors this file's existing code
    already relies on everywhere it touches a query result."""

    def __init__(self, columns, values):
        self._columns = list(columns)
        self._values = list(values)

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._columns.index(key)]
        return self._values[key]

    def keys(self):
        return self._columns

    def __iter__(self):
        return iter(self._values)

    def __repr__(self):
        return f"TursoRow({dict(zip(self._columns, self._values))})"


class TursoCursorResult:
    """Mimics the small subset of sqlite3.Cursor this file uses:
    .fetchall() and .fetchone()."""

    def __init__(self, result_set):
        columns = getattr(result_set, 'columns', []) or []
        self._rows = [TursoRow(columns, list(r)) for r in result_set.rows]
        self.last_insert_rowid = getattr(result_set, 'last_insert_rowid', None)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class TursoConnection:
    """Mimics sqlite3.Connection's .execute()/.commit()/.close() — but
    UNLIKE the previous version of this class, creates a BRAND NEW
    libsql_client for every single get_db() call, instead of sharing
    ONE persistent client across the entire app's lifetime.

    WHY THE CHANGE: sharing one long-lived client (with its own
    internal background thread + asyncio event loop) across every
    request in a multi-worker gunicorn process turned out to hang
    EVERY query in Render's environment specifically (confirmed: an
    isolated standalone script with a FRESH client succeeded instantly
    with the exact same URL/token, while the app's shared client hung
    on literally every call, including the simplest possible query).
    Creating a fresh, short-lived client per request costs a small
    amount of extra connection-setup time per call, but completely
    avoids whatever shared-state/threading interaction was breaking
    things — and this is exactly how the original sqlite3.connect()
    version of get_db() always worked anyway (a fresh connection each
    time), so this restores that same simple, safe pattern.
    """

    _query_executor = ThreadPoolExecutor(max_workers=4)
    _QUERY_TIMEOUT_SECONDS = 15

    def __init__(self):
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            self._client = libsql_client.create_client_sync(
                url=TURSO_DATABASE_URL,
                auth_token=TURSO_AUTH_TOKEN,
            )

    def execute(self, stmt, params=None):
        def _do_query():
            self._ensure_client()
            return self._client.execute(stmt, list(params) if params else [])

        future = TursoConnection._query_executor.submit(_do_query)
        try:
            result = future.result(timeout=TursoConnection._QUERY_TIMEOUT_SECONDS)
        except FutureTimeoutError:
            raise TimeoutError(
                f"Turso query timed out after {TursoConnection._QUERY_TIMEOUT_SECONDS}s "
                f"(network hiccup) — query: {stmt[:80]}"
            )
        return TursoCursorResult(result)

    def commit(self):
        pass  # Turso commits each statement immediately over the wire — no-op for API compatibility

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass  # already closed / never fully opened — safe to ignore
            self._client = None


def get_db():
    if USE_TURSO:
        return TursoConnection()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL DEFAULT 'General',
            title TEXT NOT NULL,
            note_text TEXT NOT NULL,
            processed_text TEXT,
            mermaid_code_si TEXT,
            mermaid_code_en TEXT,
            mode TEXT DEFAULT 'full',
            created_at TEXT NOT NULL
        )
    """)
    # Added later: which device/browser saved this note, so the admin
    # can see who created it. ALTER TABLE ... ADD COLUMN is wrapped in
    # try/except since SQLite has no "IF NOT EXISTS" for columns, and
    # this runs on every startup — the second and later times, the
    # column already exists and the ALTER just fails harmlessly.
    for column_def in ["anon_id TEXT", "user_name TEXT"]:
        try:
            conn.execute(f"ALTER TABLE notes ADD COLUMN {column_def}")
        except Exception:
            pass  # column already exists
    # Added later: which Google account (if any) owns this note. NULL
    # means it's a guest-saved note — those keep behaving EXACTLY as
    # before (visible in the shared/global Library listing to everyone,
    # no privacy filtering). A note WITH an owner_google_id is private
    # to that signed-in account and syncs across their devices.
    try:
        conn.execute("ALTER TABLE notes ADD COLUMN owner_google_id TEXT")
    except Exception:
        pass  # column already exists
    # NEW: stores the ORIGINAL uploaded photo (as a compressed base64
    # data URL) alongside a note, so a student can look back at the
    # actual page they photographed later — not just the extracted
    # text. Stored in the DATABASE (Turso, persistent) rather than as
    # a file on Render's local disk, because Render's local filesystem
    # is wiped on every redeploy/restart — a saved file would silently
    # vanish the next time the app deploys, but a database row survives.
    try:
        conn.execute("ALTER TABLE notes ADD COLUMN source_image_data TEXT")
    except Exception:
        pass  # column already exists
    # Lightweight, anonymous usage tracking for the admin dashboard —
    # NOT a real login/account system. "anon_id" is a random ID the
    # browser generates once and stores in localStorage (so the same
    # device is recognized across visits), paired with whatever
    # display name the person entered in the app (if any). No
    # passwords, emails, or other personal data are collected.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anon_id TEXT NOT NULL,
            user_name TEXT,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    # Added later: a short, human-readable device/browser summary
    # (e.g. "Samsung Internet · Android · Mobile"), derived from the
    # request's User-Agent header at track time — lets the admin see
    # which devices/browsers are actually being used, for spotting
    # patterns like "reports of slowness are mostly one browser".
    try:
        conn.execute("ALTER TABLE usage_events ADD COLUMN device_info TEXT")
    except Exception:
        pass  # column already exists
    # Added later: the signed-in Google account's email at the time of
    # this event (NULL for guests, or for events logged before this
    # column existed) — lets the admin see which real account a device
    # belongs to, not just an anonymous ID.
    try:
        conn.execute("ALTER TABLE usage_events ADD COLUMN user_email TEXT")
    except Exception:
        pass  # column already exists
    # Tracks the latest known "coins" balance the frontend reports for
    # each device — coins themselves live in the browser's
    # localStorage (there's no real spend/earn logic server-side yet),
    # this table just mirrors the last-seen value so the admin can see
    # it without needing real accounts.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            anon_id TEXT PRIMARY KEY,
            coins INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    # Lets the admin push a short message that shows up as a
    # notification badge for everyone using the app (checked
    # periodically from the frontend) — a simple one-way broadcast,
    # not per-user messaging.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    # Added later: if set, this announcement is PRIVATE — only the one
    # device with this anon_id will ever see it. NULL (the default,
    # unchanged from before) means a broadcast everyone sees, exactly
    # as announcements worked originally.
    try:
        conn.execute("ALTER TABLE announcements ADD COLUMN target_anon_id TEXT")
    except Exception:
        pass  # column already exists
    # Real signed-in accounts (Google Sign-In). Guests who never log in
    # never get a row here — their coins/streak/profile stay exactly as
    # before (device-local, localStorage only). Once someone signs in,
    # their coins/streak/name/picture live HERE instead, so the same
    # values follow them to any device they log into.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            google_id TEXT PRIMARY KEY,
            email TEXT,
            name TEXT,
            picture TEXT,
            coins INTEGER DEFAULT 100,
            streak INTEGER DEFAULT 0,
            last_streak_date TEXT,
            created_at TEXT,
            last_login TEXT
        )
    """)
    # Logs every successful Gemini API call — lets the admin see how
    # close usage is getting to the free-tier monthly quota, well
    # before it actually runs out.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gemini_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL
        )
    """)
    # NEW: lets admin ban an abusive device (by anon_id) or account (by
    # email) — a banned identity can't process notes or generate audio
    # anymore, but their existing saved notes aren't deleted.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS banned_identities (
            identity TEXT PRIMARY KEY,
            identity_type TEXT NOT NULL,
            reason TEXT,
            banned_at TEXT NOT NULL
        )
    """)
    # NEW: lets a student flag a note in the (shared, guest-visible)
    # Library as inappropriate/spam — admin reviews these in a
    # dedicated "Reports" section instead of having to browse the
    # entire library looking for problems.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            dismissed INTEGER DEFAULT 0
        )
    """)
    # NEW: an announcement with a future scheduled_at won't show to
    # students until that time arrives — NULL (default) means "show
    # immediately", exactly like before.
    try:
        conn.execute("ALTER TABLE announcements ADD COLUMN scheduled_at TEXT")
    except Exception:
        pass  # column already exists
    conn.commit()
    conn.close()


init_db()

# ========================================
# FEATURE 2: ABUSE PREVENTION / SAFETY VALIDATION
# ========================================
MAX_TEXT_LENGTH = 2000


def validate_text_length(text):
    if len(text) > MAX_TEXT_LENGTH:
        return (
            f'සටහන ඉතා දිගයි — අකුරු {MAX_TEXT_LENGTH}ක සීමාවක් තිබේ '
            f'(දැනට අකුරු {len(text)}ක් ඇත). කරුණාකර සටහන කෙටි කර නැවත උත්සාහ කරන්න.'
        )
    return None


# ========================================
# API RATE LIMITING (protects the free Gemini/Vision quota from being
# exhausted by one misbehaving device — e.g. a stuck retry loop)
# ========================================
# Simple in-memory sliding-window limiter, keyed per signed-in account
# (or IP address for guests). NOTE: since Render runs 2 gunicorn worker
# PROCESSES, each has its own separate memory — a device's requests
# could land on either worker, so the real effective limit across both
# workers combined is up to ~2x the number configured below. That's an
# acceptable approximation for protecting against runaway/bot usage;
# it's not meant to be a precise per-second guarantee.
_rate_limit_lock = threading.Lock()
_rate_limit_buckets = defaultdict(deque)


def _check_rate_limit(key, max_requests, window_seconds):
    """Returns (allowed: bool, retry_after_seconds: int). retry_after
    is how long until the oldest request in the window expires — i.e.
    the soonest another attempt could succeed."""
    now = time.time()
    with _rate_limit_lock:
        bucket = _rate_limit_buckets[key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= max_requests:
            retry_after = max(1, int(window_seconds - (now - bucket[0])))
            return False, retry_after
        bucket.append(now)
        return True, 0


def rate_limited(max_requests, window_seconds):
    """Decorator — apply to any route that hits a paid/quota-limited
    API (Gemini, Google Cloud Vision) to cap how often one device can
    call it. Shows the person exactly how many seconds until they can
    try again, instead of a vague 'wait a bit'."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            identifier = session.get('user_id') or request.remote_addr or 'unknown'
            bucket_key = f"{f.__name__}:{identifier}"
            allowed, retry_after = _check_rate_limit(bucket_key, max_requests, window_seconds)
            if not allowed:
                mins, secs = divmod(retry_after, 60)
                if mins > 0:
                    wait_str = f'මිනිත්තු {mins}ක් {secs} තත්පරයක්' if secs else f'මිනිත්තු {mins}ක්'
                else:
                    wait_str = f'තත්පර {secs}ක්'
                return jsonify({
                    'status': 'error',
                    'message': f'ඉතා වේගවත් ලෙස requests යවනවා — {wait_str} ඉන්න, ඊට පස්සේ නැවත උත්සාහ කරන්න.',
                    'retry_after_seconds': retry_after,
                }), 429
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ========================================
# GOOGLE CLOUD VISION API (OCR)
# ========================================
CLOUD_OCR_AVAILABLE = False
try:
    from google.cloud import vision

    credentials_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
    print(f"🔍 Credentials path: {credentials_path}")

    if credentials_path and os.path.exists(credentials_path):
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = credentials_path
        vision_client = vision.ImageAnnotatorClient()
        CLOUD_OCR_AVAILABLE = True
        print("✅ Google Cloud Vision API is ready!")
    else:
        print("⚠️ Google Cloud Vision credentials not found")
except Exception as e:
    print(f"⚠️ Google Cloud Vision not available: {e}")

# ========================================
# GEMINI API
# ========================================
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)
    print("✅ Gemini API is ready!")
else:
    client = None
    print("⚠️ GEMINI_API_KEY not found")

SAFETY_SETTINGS = [
    types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='BLOCK_MEDIUM_AND_ABOVE'),
    types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='BLOCK_MEDIUM_AND_ABOVE'),
    types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='BLOCK_MEDIUM_AND_ABOVE'),
    types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='BLOCK_MEDIUM_AND_ABOVE'),
]


def parse_device_info(user_agent):
    """Turns a raw User-Agent string into a short, readable summary like
    'Samsung Internet · Android · Mobile' or 'Chrome · Windows · Desktop'.
    This is a lightweight, regex-based heuristic (no external library) —
    it won't be 100% perfect for every obscure browser/device, but it
    covers the common cases well enough for the admin dashboard to be
    useful for spotting patterns (e.g. "most of our slow-device reports
    are Samsung Internet on Android").

    ORDER MATTERS below: Samsung Internet's UA also contains the word
    "Chrome" (since it's Chromium-based), and new Edge's UA also
    contains "Chrome" and "Safari" — so the more specific/newer browser
    checks must run BEFORE the generic ones they'd otherwise be
    misidentified as.
    """
    if not user_agent:
        return 'Unknown'

    ua = user_agent

    # ---- Browser name (most specific first) ----
    if 'SamsungBrowser' in ua:
        browser = 'Samsung Internet'
    elif 'EdgA' in ua or 'EdgiOS' in ua or 'Edg/' in ua:
        browser = 'Edge'
    elif 'OPR' in ua or 'Opera' in ua:
        browser = 'Opera'
    elif 'FxiOS' in ua:
        browser = 'Firefox (iOS)'
    elif 'Firefox' in ua:
        browser = 'Firefox'
    elif 'CriOS' in ua:
        browser = 'Chrome (iOS)'
    elif 'Chrome' in ua:
        browser = 'Chrome'
    elif 'Safari' in ua and 'Version' in ua:
        browser = 'Safari'
    else:
        browser = 'Other'

    # ---- Operating system ----
    if 'Android' in ua:
        os_name = 'Android'
    elif 'iPhone' in ua or 'iPad' in ua or 'iPod' in ua:
        os_name = 'iOS'
    elif 'Windows' in ua:
        os_name = 'Windows'
    elif 'Mac OS X' in ua or 'Macintosh' in ua:
        os_name = 'Mac'
    elif 'Linux' in ua:
        os_name = 'Linux'
    else:
        os_name = 'Unknown'

    # ---- Device type ----
    if 'iPad' in ua or ('Android' in ua and 'Mobile' not in ua):
        device_type = 'Tablet'
    elif 'Mobile' in ua or 'iPhone' in ua or 'Android' in ua:
        device_type = 'Mobile'
    else:
        device_type = 'Desktop'

    return f'{browser} · {os_name} · {device_type}'


def detect_language(text):
    sinhala_count = 0
    telugu_count = 0
    tamil_count = 0
    english_count = 0

    for char in text:
        code = ord(char)
        if 0x0D80 <= code <= 0x0DFF:
            sinhala_count += 1
        elif 0x0C00 <= code <= 0x0C7F:
            telugu_count += 1
        elif 0x0B80 <= code <= 0x0BFF:
            tamil_count += 1
        elif (0x0041 <= code <= 0x005A) or (0x0061 <= code <= 0x007A):
            english_count += 1

    total = sinhala_count + telugu_count + tamil_count + english_count
    if total == 0:
        return 'Unknown'

    if sinhala_count > telugu_count and sinhala_count > tamil_count and sinhala_count > english_count:
        return 'Sinhala'
    elif telugu_count > sinhala_count and telugu_count > tamil_count and telugu_count > english_count:
        return 'Telugu'
    elif tamil_count > sinhala_count and tamil_count > telugu_count and tamil_count > english_count:
        return 'Tamil'
    elif english_count > sinhala_count and english_count > telugu_count and english_count > tamil_count:
        return 'English'
    else:
        return 'Mixed'


def format_for_podcast(text):
    if not text:
        return ""

    lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
    lines = [ln if ln[-1] in ['.', '!', '?', '।', ':'] else ln + '.' for ln in lines]
    text = ' '.join(lines)

    text = re.sub(r'[\[\]\(\)\{\}]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()

    text = text.replace('...', '. ')
    text = text.replace('..', '. ')

    try:
        text = re.sub(r'(\d+)([\u0D80-\u0DFF])', r'\1 \2', text)
    except Exception:
        pass

    text = re.sub(r'\s+', ' ', text).strip()

    if text and text[-1] not in ['.', '!', '?']:
        text += '.'

    return text


def cleanup_old_audio(max_age_seconds=3600):
    try:
        now = time.time()
        for path in glob.glob('static/output_*.mp3'):
            if now - os.path.getmtime(path) > max_age_seconds:
                os.remove(path)
    except Exception as e:
        print(f"⚠️ Audio cleanup skipped: {e}")


GTTS_AVAILABLE = True
GTTS_FRAME_RATE = 24000


def split_into_sentences(text):
    parts = re.split(r'(?<=[.!?])\s+', text)
    return [p.strip() for p in parts if p.strip()]


def split_into_clauses(sentence, max_words=14):
    """Splits a single sentence further at commas/semicolons — these are
    NOT separate 'sentences' for highlight-timing purposes (the frontend
    still highlights the whole original sentence as one unit), but
    giving each clause its own tiny breathing pause during synthesis
    makes long sentences sound much less rushed/robotic, closer to how
    a person naturally pauses briefly at a comma before continuing.

    SAFETY NET: if a clause has no comma/semicolon at all (or the AI's
    system-prompt instruction to add natural breathing commas didn't
    get followed for some reason — or this is Full Text Mode, using the
    student's own raw text as-is, which the system instruction never
    even sees) and ends up longer than max_words, it gets force-split at
    a word boundary anyway. Returns a list of (clause_text, is_forced)
    tuples — 'is_forced' clauses get a shorter, more subtle pause than a
    real punctuation-based clause break (see clause_pause_ms handling in
    synthesize_gtts_natural), since there's no actual grammatical pause
    there — just a practical breath before the sentence runs on too
    long."""
    raw_parts = re.split(r'(?<=[,;])\s+', sentence)
    final_parts = []
    for part in raw_parts:
        words = part.split()
        if len(words) <= max_words:
            final_parts.append((part, False))
        else:
            for i in range(0, len(words), max_words):
                chunk = ' '.join(words[i:i + max_words]).strip()
                if chunk:
                    final_parts.append((chunk, True))
    return [(p, forced) for (p, forced) in final_parts if p.strip()]


def _tts_sentence_to_segment(args):
    sentence, lang = args
    buf = io.BytesIO()
    gTTS(text=sentence, lang=lang, slow=False, tld='com').write_to_fp(buf)
    buf.seek(0)
    segment = AudioSegment.from_file(buf, format='mp3')
    return segment.set_frame_rate(GTTS_FRAME_RATE).set_channels(1)


def synthesize_gtts_natural(text, lang='si', pause_ms=350, paragraph_pause_ms=600,
                             clause_pause_ms=140, forced_break_pause_ms=90, max_workers=5):
    """Returns (combined_audio, sentence_timings) where sentence_timings
    is a list of {"text": str, "start": float, "end": float} in
    SECONDS — the real, measured duration of each sentence's own TTS
    segment (not a naive equal-split-across-total-duration guess).
    This lets the frontend highlight whichever sentence is actually
    playing at any given moment, instead of assuming every line/
    sentence takes the same amount of time to read aloud (which was
    very inaccurate for a mix of short and long sentences).

    NEW (naturalness tuning): each sentence is further split into
    comma/semicolon-delimited CLAUSES, synthesized individually, and
    stitched back together with a short pause between them — a real
    comma-based clause gets clause_pause_ms, while a clause that had to
    be force-split purely because it ran too long with NO punctuation
    at all gets a shorter forced_break_pause_ms (a subtle breath, not an
    obvious comma pause, since grammatically there wasn't one there).
    Sentence-level highlight timing is unaffected (still one timing
    entry per original full sentence) — only the INTERNAL pacing of
    longer sentences changes."""
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()] or [text]

    flat_sentences = []
    for p_index, paragraph in enumerate(paragraphs):
        sentences = split_into_sentences(paragraph) or [paragraph]
        for s_index, sentence in enumerate(sentences):
            is_last_in_paragraph = (s_index == len(sentences) - 1)
            flat_sentences.append((sentence, is_last_in_paragraph))
    if not flat_sentences:
        flat_sentences = [(text, True)]

    # Pre-split every sentence into its clauses (each tagged with
    # whether the break was a real punctuation break or a forced
    # word-count break), and build ONE flat task list across ALL
    # clauses of ALL sentences — keeping the thread pool working on the
    # smallest possible units in parallel, rather than looping
    # sentence-by-sentence (which would serialize each sentence's own
    # clause calls one after another).
    sentence_clause_lists = [split_into_clauses(s) or [(s, False)] for (s, _) in flat_sentences]
    tasks = []
    task_owner = []  # parallel list: which (sentence_idx, clause_idx) each task belongs to
    task_is_forced = []  # whether THIS clause was a forced word-count split
    for sentence_idx, clauses in enumerate(sentence_clause_lists):
        for clause_idx, (clause_text, is_forced) in enumerate(clauses):
            tasks.append((clause_text, lang))
            task_owner.append((sentence_idx, clause_idx))
            task_is_forced.append(is_forced)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        clause_segments_flat = list(executor.map(_tts_sentence_to_segment, tasks))

    # Regroup the flat clause segments back under their owning sentence,
    # in original clause order, then stitch each sentence's own clauses
    # together with the appropriate pause (real comma vs. forced break).
    clause_silence = AudioSegment.silent(duration=clause_pause_ms, frame_rate=GTTS_FRAME_RATE)
    forced_silence = AudioSegment.silent(duration=forced_break_pause_ms, frame_rate=GTTS_FRAME_RATE)
    segments_by_sentence = [[] for _ in flat_sentences]
    for (sentence_idx, clause_idx), seg, is_forced in zip(task_owner, clause_segments_flat, task_is_forced):
        segments_by_sentence[sentence_idx].append((clause_idx, seg, is_forced))

    segments = []
    for sentence_idx in range(len(flat_sentences)):
        clause_entries = sorted(segments_by_sentence[sentence_idx], key=lambda x: x[0])
        sentence_audio = AudioSegment.silent(duration=0, frame_rate=GTTS_FRAME_RATE)
        for i, (_, clause_seg, is_forced) in enumerate(clause_entries):
            sentence_audio += clause_seg
            if i != len(clause_entries) - 1:
                # The pause AFTER this clause depends on whether the NEXT
                # clause's break was forced (word-count) or a real comma —
                # using is_forced of the clause that just ended is close
                # enough in practice since forced breaks are symmetric.
                sentence_audio += forced_silence if is_forced else clause_silence
        segments.append(sentence_audio)

    sentence_silence = AudioSegment.silent(duration=pause_ms, frame_rate=GTTS_FRAME_RATE)
    paragraph_silence = AudioSegment.silent(duration=paragraph_pause_ms, frame_rate=GTTS_FRAME_RATE)
    combined = AudioSegment.silent(duration=0, frame_rate=GTTS_FRAME_RATE)

    sentence_timings = []
    total = len(flat_sentences)
    for i, ((sentence_text, is_last_in_paragraph), segment) in enumerate(zip(flat_sentences, segments)):
        start_ms = len(combined)
        combined += segment
        end_ms = len(combined)
        sentence_timings.append({
            'text': sentence_text,
            'start': round(start_ms / 1000, 3),
            'end': round(end_ms / 1000, 3),
        })
        if i == total - 1:
            break
        combined += paragraph_silence if is_last_in_paragraph else sentence_silence

    return combined, sentence_timings


GEMINI_TTS_VOICES = {
    'si': 'Leda',   # default to female voice if none chosen
    'ta': 'Leda',
    'en': 'Leda',
}

# Simplified to just two curated choices — a clear Male and Female
# newscaster-style voice, instead of overwhelming students with all 30
# of Gemini's built-in options. Voice names are identical across both
# model versions below, so no extra mapping needed there.
GEMINI_TTS_VOICE_OPTIONS = {
    'Sadaltager': 'Male',
    'Leda': 'Female',
}

# Two selectable model generations — 3.1 is a real generational upgrade
# (lower latency, more expressive/natural, richer style control) but
# costs 2x as much per output token ($20/1M vs $10/1M), so it's offered
# as a distinct, separately-priced choice rather than silently
# replacing 2.5.
GEMINI_TTS_MODEL_VERSIONS = {
    'v25': 'gemini-2.5-flash-preview-tts',
    'v31': 'gemini-3.1-flash-tts-preview',
}


def calculate_gemini_tts_coin_cost(text_length, model_version='v25'):
    """Tiered pricing for Gemini (Natural AI) TTS generation, based on
    script length AND which model version was used — real Gemini API
    cost scales with audio duration (roughly proportional to text
    length), so a flat per-generation price wouldn't be fair: a short
    note and a full 2000-character chapter cost very different amounts
    to actually generate. The 3.1 model also costs exactly 2x as much
    per output token as 2.5 ($20/1M vs $10/1M), so its coin tiers are
    scaled up to match.

    v25 tiers (character count of the SCRIPT, capped app-wide at
    MAX_TEXT_LENGTH=2000):
      0-500     -> 5 coins   (~50s audio, real cost ≈ $0.0125 / Rs 4)
      501-1200  -> 12 coins  (~2min audio, real cost ≈ $0.03 / Rs 9)
      1201+     -> 20 coins  (~3min+ audio, real cost ≈ $0.05 / Rs 15)
    v31 tiers are exactly double, matching its 2x per-token price."""
    if text_length <= 500:
        base = 5
    elif text_length <= 1200:
        base = 12
    else:
        base = 20
    return base * 2 if model_version == 'v31' else base


# ========================================
# GEMINI TTS CALL GATE (queue/throttle)
# ========================================
# WHY: Google's rate limit for the TTS models is only 10 requests per
# minute (RPM) at our current billing tier — if several students click
# "Generate Audio" (Natural AI) around the same time, the 11th+ request
# in that minute gets flatly REJECTED by Google with a 429 error.
#
# This tracks how many Gemini TTS calls THIS worker process has made
# in the last 60 seconds. If we're near the safety threshold, new
# requests WAIT (sleep in short increments) for a slot to free up,
# instead of firing immediately and risking a hard rejection. From the
# student's perspective, the request just takes a bit longer — the
# frontend shows friendly rotating "in progress" messages the whole
# time (see the JS side), so it never looks like a failure.
#
# NOTE: this is a PER-WORKER-PROCESS gate (in-memory, not shared across
# gunicorn's multiple worker processes). With --workers 2, the
# practical safety margin is set conservatively (4 per worker) so the
# worst-case combined total across both workers still stays safely
# under Google's shared 10 RPM limit.
_gemini_tts_call_times = deque()
_gemini_tts_gate_lock = threading.Lock()
GEMINI_TTS_SAFE_RPM_PER_WORKER = 4
# FIX (502 errors — worker timeout regression): the queue wait
# (previously up to 45s) combined with the Gemini call's own timeout
# (90s) could total 135s on a SINGLE attempt alone — already past
# gunicorn's 120s worker timeout, before even considering that this
# function retries up to 4 times. Reduced to a much safer cap, and a
# hard TOTAL TIME BUDGET (see below) now bounds the whole function
# regardless of how queueing and retries combine.
GEMINI_TTS_MAX_QUEUE_WAIT_SECONDS = 10


def _gemini_tts_call_gate():
    """Blocks (in short sleeps) until it's safe to make another Gemini
    TTS call, or gives up after GEMINI_TTS_MAX_QUEUE_WAIT_SECONDS (at
    which point the caller proceeds anyway — the existing retry logic
    is the backstop if Google still rejects it)."""
    waited = 0
    while waited < GEMINI_TTS_MAX_QUEUE_WAIT_SECONDS:
        with _gemini_tts_gate_lock:
            now = time.time()
            while _gemini_tts_call_times and now - _gemini_tts_call_times[0] > 60:
                _gemini_tts_call_times.popleft()
            if len(_gemini_tts_call_times) < GEMINI_TTS_SAFE_RPM_PER_WORKER:
                _gemini_tts_call_times.append(now)
                return
        time.sleep(2)
        waited += 2
    # Gave up waiting — record the attempt anyway and proceed; the
    # per-request retry logic in synthesize_gemini_tts will catch a
    # 429 if Google does reject it.
    with _gemini_tts_gate_lock:
        _gemini_tts_call_times.append(time.time())


def synthesize_gemini_tts(text, lang='si', voice_name=None, model_version='v25'):
    """Generates natural-sounding audio using Gemini's native TTS model
    — a genuinely different voice engine from gTTS (LLM-driven, not a
    classic speech synthesizer), currently in Preview.

    SPEED: sends the ENTIRE script as ONE single Gemini TTS request —
    exactly how Google AI Studio's own Playground does it, which is
    why testing directly there feels fast. An earlier version of this
    function split the script into paragraphs and ran multiple
    SEPARATE calls (even in parallel, batched 2-at-a-time), but that
    turned out to be SLOWER overall for typical multi-paragraph notes:
    each separate API call pays its own per-request setup/network
    overhead, and with only 2 running at once, a note with e.g. 5
    paragraphs needed 3 sequential batches — compounding that overhead
    instead of paying it once. One request for the whole script avoids
    all of that.

    NOTE on sentence timings: Gemini TTS doesn't return per-sentence
    boundaries, so timings are ESTIMATED by distributing the total
    measured audio duration proportionally across sentences by
    character count — an approximation, not exact, but close enough
    for karaoke-style highlighting in practice.
    """
    if not client:
        raise GeminiGenerationError("Gemini API is not configured (missing GEMINI_API_KEY).")

    # NEW: person can pick Male (Sadaltager) or Female (Leda); falls
    # back to a sensible default if they didn't (or picked an invalid
    # value).
    if voice_name not in GEMINI_TTS_VOICE_OPTIONS:
        voice_name = GEMINI_TTS_VOICES.get(lang, 'Leda')

    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()] or [text]
    flat_sentences = []
    for paragraph in paragraphs:
        sentences = split_into_sentences(paragraph) or [paragraph]
        flat_sentences.extend(sentences)
    if not flat_sentences:
        flat_sentences = [text]

    # Director's notes: fixed Style (Empathetic) + Pace (Natural), per
    # Gemini TTS's own prompting convention. Docs recommend keeping
    # these instructions in English even when the transcript itself is
    # in another language (Sinhala/Tamil), for best results.
    directed_prompt = (
        "Style: Empathetic — warm, caring, and encouraging delivery, "
        "like a supportive teacher patiently explaining something to a student. "
        "IMPORTANT: speak clearly and audibly at a normal, full speaking volume — "
        "NOT a whisper, NOT hushed or breathy, every word should be crisp and easy to hear.\n"
        "Pace: Natural, comfortable speaking speed — not rushed, not slow.\n\n"
        f"{text}"
    )

    # FIX (worker SIGKILL crash): a Gemini TTS call can occasionally
    # hang waiting on a network response that never arrives. Without an
    # explicit timeout here, that hang runs past gunicorn's own 120s
    # worker timeout, at which point gunicorn forcibly SIGKILLs the
    # entire worker process — the client gets an empty/broken 500
    # response with no useful error message (this is what caused the
    # 'JSON.parse SyntaxError' bug on the frontend). 90s here is safely
    # under that 120s limit, so if Gemini stalls, OUR code raises a
    # clean, catchable timeout exception well before gunicorn has to
    # intervene.
    #
    # FIX (no retry resilience under concurrent load): this call used
    # to have NO retry logic at all — unlike script generation
    # (call_gemini_structured), a single transient hiccup OR a 429
    # rate-limit (much more likely when several people generate audio
    # around the same time, since the Gemini API quota is shared
    # across the whole app, not per-device) failed the request
    # immediately with zero attempt to recover. Now retries both
    # transient overload errors AND rate-limit errors, same pattern as
    # script generation.
    # FIX (502 errors — worker timeout regression): reduced from 90s to
    # 60s per call. A hard TOTAL TIME BUDGET below now bounds the
    # ENTIRE function (queue waits + all call attempts + retry sleeps
    # combined) to stay safely under gunicorn's 120s worker timeout —
    # previously each of these had its own separate cap, but nothing
    # stopped them from ADDING UP past 120s across multiple retries.
    max_tts_retries = 4
    response = None
    last_tts_error = None
    synth_start_time = time.time()
    TOTAL_TIME_BUDGET_SECONDS = 95  # hard ceiling, safely under gunicorn's 120s timeout

    for attempt in range(1, max_tts_retries + 1):
        elapsed = time.time() - synth_start_time
        if elapsed > TOTAL_TIME_BUDGET_SECONDS:
            print(f"⚠️ Gemini TTS aborting — total time budget ({TOTAL_TIME_BUDGET_SECONDS}s) exceeded after {elapsed:.1f}s")
            raise GeminiGenerationError("Gemini TTS timed out across retries (server-side time budget exceeded).")

        _gemini_tts_call_gate()  # queue/wait for a safe slot before calling Gemini
        try:
            response = client.models.generate_content(
                model=GEMINI_TTS_MODEL_VERSIONS.get(model_version, GEMINI_TTS_MODEL_VERSIONS['v25']),
                contents=directed_prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                        )
                    ),
                    http_options=types.HttpOptions(timeout=60_000),  # milliseconds
                )
            )
            break  # success
        except Exception as e:
            last_tts_error = e
            if _is_rate_limit_error(e) and attempt < max_tts_retries:
                print(f"⚠️ Gemini TTS rate-limited (attempt {attempt}), retrying in 5s: {e}")
                time.sleep(5)
                continue
            if _is_transient_gemini_error(e) and attempt < max_tts_retries:
                wait_seconds = min(attempt * 3, 8)
                print(f"⚠️ Gemini TTS transient error (attempt {attempt}), retrying in {wait_seconds}s: {e}")
                time.sleep(wait_seconds)
                continue
            raise
    if response is None:
        raise last_tts_error or GeminiGenerationError("Gemini TTS failed after retries.")

    if not getattr(response, 'candidates', None):
        raise GeminiGenerationError("Gemini TTS returned no audio (possibly blocked by safety filters).")

    audio_data = response.candidates[0].content.parts[0].inline_data.data
    # Gemini TTS returns raw 24kHz 16-bit mono PCM — wrap it as an
    # AudioSegment the same way the rest of this file already handles
    # audio (so export/duration/etc. all work identically to gTTS output).
    audio_segment = AudioSegment(
        data=audio_data,
        sample_width=2,
        frame_rate=24000,
        channels=1,
    )

    total_duration_ms = len(audio_segment)
    total_chars = sum(len(s) for s in flat_sentences) or 1
    sentence_timings = []
    cursor_ms = 0
    for sentence_text in flat_sentences:
        share = len(sentence_text) / total_chars
        duration_ms = total_duration_ms * share
        start_ms = cursor_ms
        end_ms = cursor_ms + duration_ms
        sentence_timings.append({
            'text': sentence_text,
            'start': round(start_ms / 1000, 3),
            'end': round(end_ms / 1000, 3),
        })
        cursor_ms = end_ms

    return audio_segment, sentence_timings


class GeminiGenerationError(Exception):
    pass


def build_system_instruction(output_language):
    """Returns the full Gemini system instruction, with the podcast
    script's language rule swapped based on the person's chosen OUTPUT
    language — independent of whatever language the note itself was
    written in. Previously the instruction always told Gemini to match
    the note's own language; now the person explicitly picks Sinhala or
    English for the narration/script output via a toggle in the UI, and
    Gemini is told to always write in that language regardless of the
    input note's language. The mind maps (mermaid_code_si/en) are
    unaffected — both language versions are still always generated.
    """
    if output_language == 'en':
        json_line = (
            '  "podcast_script": "<full podcast script, written entirely in English '
            'regardless of the note\'s own language>",'
        )
        language_rule = (
            '2. **භාෂාව (Output Language):** "podcast_script" එක **සම්පූර්ණයෙන්ම '
            'English භාෂාවෙන්ම** ලියන්න — පාඩම Sinhala, Tamil, හෝ English කුමන '
            'භාෂාවකින් ලියා තිබුණත්, output එක සැමවිටම English විය යුතුය (පාඩමේ '
            'භාෂාව මෙතනදී අදාළ නොවේ). ස්වාභාවික, කථනාත්මක, podcast-style English '
            'වචන/expressions ("you know", "let\'s dive in", "here\'s the thing", '
            '"next up") යොදාගෙන ගලායන කතාවක් ලෙස ලියන්න, නිවැරදි English '
            'ව්‍යාකරණයෙන්.'
        )
    else:  # 'si' (default)
        json_line = (
            '  "podcast_script": "<සම්පූර්ණ පොඩ්කාස්ට් script එක, සම්පූර්ණයෙන්ම '
            'සිංහල භාෂාවෙන්ම>",'
        )
        language_rule = (
            '2. **භාෂාව (Output Language):** "podcast_script" එක **සම්පූර්ණයෙන්ම '
            'සිංහල භාෂාවෙන්ම** ලියන්න — පාඩම Sinhala, Tamil, හෝ English කුමන '
            'භාෂාවකින් ලියා තිබුණත්, output එක සැමවිටම සිංහල විය යුතුය (පාඩමේ '
            'භාෂාව මෙතනදී අදාළ නොවේ). "යාලුවනේ", "ඔයාලා දන්නවාද?", "ඊළඟට", "මෙන්න", '
            '"දැන් බලමු" වැනි වදන් යොදාගෙන ගලායන කතාවක් ලෙස ලියන්න, නිවැරදි සිංහල '
            'ව්‍යාකරණයෙන් සහ අක්ෂර වින්‍යාසයෙන්.'
        )

    instruction = SYSTEM_INSTRUCTION
    instruction = instruction.replace(
        '  "podcast_script": "<සම්පූර්ණ පොඩ්කාස්ට් script එක, පාඩම ලියැවී ඇති භාෂාවෙන්ම (Sinhala නම් Sinhala, Tamil නම් Tamil)>",',
        json_line
    )
    instruction = instruction.replace(
        '2. **භාෂාව:** පාඩම ලියැවී ඇති භාෂාවෙන්ම ලියන්න — පාඩම Sinhala නම් Sinhala වලින්, Tamil\n'
        '   නම් Tamil වලින් (පාඩම mix වී ඇත්නම් වැඩිපුර පවතින භාෂාව භාවිත කරන්න). Sinhala\n'
        '   ලියද්දී "යාලුවනේ", "ඔයාලා දන්නවාද?", "ඊළඟට", "මෙන්න", "දැන් බලමු" වැනි වදන්\n'
        '   යොදාගෙන ගලායන කතාවක් ලෙස ලියන්න. Tamil ලියද්දී ඒ හා සමාන ස්වාභාවික, කථනාත්මක,\n'
        '   podcast-style Tamil වචන/සිතුවිලි යොදාගෙන ලියන්න.\n'
        '3. සිංහල හෝ Tamil ව්‍යාකරණ සහ අක්ෂර වින්‍යාසය නිවැරදිව යොදන්න.',
        language_rule
    )
    return instruction


SYSTEM_INSTRUCTION = f"""
ඔබ ශ්‍රී ලංකාවේ සිටින ඉතා දක්ෂ, සහයෝගී අධ්‍යාපන AI සහායකයෙකි. ඔබට ලැබෙන පාඩම් සටහන
(Sinhala, Tamil, හෝ English) සකසා, **JSON object එකක් විතරක්** output කරන්න — වෙන කිසිදු
text, පැහැදිලි කිරීමක්, හෝ markdown code fence (```) JSON object එකෙන් පිටත නොදාන්න:

{{
  "podcast_script": "<සම්පූර්ණ පොඩ්කාස්ට් script එක, පාඩම ලියැවී ඇති භාෂාවෙන්ම (Sinhala නම් Sinhala, Tamil නම් Tamil)>",
  "mermaid_code_si": "<Mermaid.js mindmap syntax එකක්, node labels සියල්ලම සිංහලෙන්>",
  "mermaid_code_en": "<Mermaid.js mindmap syntax එකක්, node labels සියල්ලම English වලින්, mermaid_code_si එකේම structure/nodes ම>"
}}

=== "podcast_script" සඳහා නීති ===
1. මෙය කෙටි සාරාංශයක් නොවේ. පාඩමේ ඇති සියලුම කරුණු, දිනයන්, සංඛ්‍යා, නිර්වචන, නම්,
   ස්ථාන, සිදුවීම් කිසිසේත් මඟ නොහරිමින් ඇතුළත් කරන්න.
2. **භාෂාව:** පාඩම ලියැවී ඇති භාෂාවෙන්ම ලියන්න — පාඩම Sinhala නම් Sinhala වලින්, Tamil
   නම් Tamil වලින් (පාඩම mix වී ඇත්නම් වැඩිපුර පවතින භාෂාව භාවිත කරන්න). Sinhala
   ලියද්දී "යාලුවනේ", "ඔයාලා දන්නවාද?", "ඊළඟට", "මෙන්න", "දැන් බලමු" වැනි වදන්
   යොදාගෙන ගලායන කතාවක් ලෙස ලියන්න. Tamil ලියද්දී ඒ හා සමාන ස්වාභාවික, කථනාත්මක,
   podcast-style Tamil වචන/සිතුවිලි යොදාගෙන ලියන්න.
3. සිංහල හෝ Tamil ව්‍යාකරණ සහ අක්ෂර වින්‍යාසය නිවැරදිව යොදන්න.
3a. **වාක්‍ය දිග සහ Breathing Pauses:** එක් වාක්‍යයක් වචන 20කට වඩා දිග නොවෙන්න බලන්න —
    දිග අදහසක් තිබේ නම්, එය කුඩා වාක්‍ය කිහිපයකට කඩන්න, නැත්නම් comma (,) යොදාගෙන
    ස්වාභාවික breathing point එකක් දෙන්න. **හුස්මක්වත් නොගෙන දිගටම කියවෙන ලෙස** වචන
    20+ ක් comma එකක්වත් නැතිව එක දිගට ලියන්න එපා — TTS audio එකෙන් මෙය අස්වාභාවික ලෙස
    ඇසෙනවා. සාමාන්‍යයෙන් වචන 10-15ක් පමණ තැබූ පසු comma එකකින් හෝ වාක්‍ය අවසන් කිරීමකින්
    breathing point එකක් දෙන්න.
4. **වැදගත් සීමාව:** "podcast_script" එක අකුරු {MAX_TEXT_LENGTH - 200}ක් නොඉක්මවිය
   යුතුය (spaces ඇතුළුව). පාඩම ඉතා දිග නම්, පොඩ්කාස්ට් වචන (යාලුවනේ, ඊළඟට වැනි)
   අඩුවෙන් යොදාගෙන හෝ core content එකට priority දී, එම සීමාව තුළ තබාගන්න — core
   කරුණු කිසිවක් මඟ නොහැරිය යුතුය, නමුත් filler වචන ප්‍රමාණය අවශ්‍ය නම් අඩු කරන්න.
5. **රසායන සූත්‍ර/ගණිත ලියද්දී LaTeX/math notation (\\$H_2O\\$, ^, _ subscript syntax
   වැනි) කිසිසේත් යොදන්න එපා** — මේවා TTS audio එකෙන් කියවෙන්නේ අමුතු ලෙසින්, කියවීමේදීත්
   අවුල් සහගතයි. සාමාන්‍ය plain text විතරක් යොදන්න — උදා: "ජලය (H2O)" (ජලය ($H_2O$)
   නොවේ), "කාබන් ඩයොක්සයිඩ් (CO2)" (($CO_2$) නොවේ). හැකි නම්, formula එකට වඩා සිංහල
   වචනයම (උදා: "ජලය", "කාබන් ඩයොක්සයිඩ්") පමණක් යොදන්න, formula එක අත්‍යවශ්‍ය නම්
   විතරක් වරහන් තුළ plain text එකක් ලෙස එකතු කරන්න.
6. **ඉතා වැදගත් — එකම වචනය දෙපාරක් නොකියවෙන්න:** Product/brand/technical නම් (Apple
   Watch, AirPods, iOS, watchOS වැනි) ලියද්දී, **සිංහල phonetic spelling එකක් සහ
   English මුල් නම දෙකම එකට** (උදා: "ඇපල් වොච් (Apple Watch)") කිසිසේත් **නොදෙන්න** —
   මෙය TTS audio එකෙන් **එකම නම දෙපාරක්ම** (Sinhala phonetic එකකින්, ඊට පස්සේ English
   එකෙන්ම) කියවෙන්නට හේතු වේ. ඒ වෙනුවට, English product/brand නම් **English spelling
   එකෙන්ම විතරක්** ලියන්න (උදා: "ඇපල් වොච් (Apple Watch)" නොව, "Apple Watch" විතරක්) —
   සිංහල වාක්‍ය ප්‍රවාහය තුළ English නම එකක්ම ස්වාභාවිකව embed කරන්න, phonetic
   spelling+bracket repetition එකක් නොකර.

=== "mermaid_code_si" සහ "mermaid_code_en" දෙකටම පොදු නීති ===
1. "flowchart TD" වලින්ම පටන් ගන්න.
1a. **වැදගත්:** Root node එකෙන් කෙලින්ම පටන්ගන්නා **ප්‍රධාන branch ගණන උපරිම 4කට** සීමා
    කරන්න (5, 6ක් නොවේ). Topic එකේ categories 4ට වඩා තිබේ නම්, ඒවා ලොකු branch 3-4ක්
    යටතේ **sub-groups** විදිහට nest කරන්න (depth වැඩි කර, width අඩු කර) — මෙයින් diagram
    එක අනවශ්‍ය ලෙස පළල් නොවී, සාධාරණ ලෙස උස (vertical) හැඩයක් ගනී, mobile screen එකකට
    හෝ PDF/PNG export එකකට වඩා ගැලපෙනවා.
2. පාඩමේ ප්‍රධාන මාතෘකාව root node එකක් ලෙසත්, උප මාතෘකා/ප්‍රධාන සංකල්ප child nodes
   ලෙසත් සකසන්න (අවශ්‍ය නම් උප-child nodes එකතු කරන්න), "-->" arrow මගින් සම්බන්ධ කරන්න.
3. දෙකෙහිම node structure එක (nodes ගණන, connections) එකම විය යුතුය — වෙනස් වෙන්නේ
   label භාෂාව විතරයි: "mermaid_code_si" හි සියලුම node labels සිංහලෙන්, "mermaid_code_en"
   හි සියලුම node labels English වලින්.
4. එක් node label එකක් වචන 1-6ක් තරම් කෙටියෙන් තබන්න.
4a. **රසායන සූත්‍ර/සංඛ්‍යා ලියද්දී subscript/superscript unicode අකුරු (₂, ₃, ², ³ වැනි)
    කිසිසේත් යොදන්න එපා** — ඒවා fonts වල හරියටම render නොවී, node box එකේ text එක
    cramped/cut-off වගේ පෙනී යයි. සාමාන්‍ය (regular) සංඛ්‍යා විතරක් යොදන්න — උදා: "H2O"
    (H₂O නොවේ), "CO2" (CO₂ නොවේ).
5. Node id ලෙස ඉංග්‍රීසි අකුරු/සංඛ්‍යා පමණක් යොදන්න (උදා: A, B, C1) — Sinhala අකුරු
   node id එකට කිසිසේත් යොදන්න එපා, mermaid parser එකට එය parse කරගත නොහැක. Node id
   දෙකේම (si/en) එකම විය යුතුය.
6. **Branch-based color-coding** (semantic type අනුව නොවේ — branch structure එක අනුව):
   a. Root node එකට **හැමවිටම** ":::root" class එක දෙන්න (කොළ පාට වේ) — උදා:
      "Root[Label]:::root".
   b. Root node එකෙන් කෙලින්ම පටන්ගන්නා **ප්‍රධාන branch එකක්** (root එකේ direct child
      එකක්, උදා: "කොන්දේසි", "ක්‍රියාවලිය") ට, "branch1", "branch2", "branch3",
      "branch4", "branch5", "branch6" කියන class 6න් **එකක්** දෙන්න — පළමු ප්‍රධාන
      branch එකට "branch1", දෙවෙනියට "branch2", මෙසේ පිළිවෙළින් (branch 6ට වඩා
      තිබේ නම් "branch1" සිට නැවත පටන් ගන්න).
   c. ඒ ප්‍රධාන branch එකට **යටින්ම ඇති සියලුම child/grandchild nodes** (එනම් එම
      branch එකේ sub-nodes ඔක්කොම, කොපමණ level ගැඹුරු වුවත්) ට, **එම branch එකේම
      "light" version එක** දෙන්න — උදා: "branch1" එකට යටින් ඇති nodes ඔක්කොම
      "branch1light" class එක ගත යුතුය (parent branch එකේම color එකේ **halu/light
      shade එකක්**, වෙනස් color එකක් නොවේ).
   d. එකම branch එකේ සියලුම nodes (child, grandchild, ...) එකම light-class එකම
      ගත යුතුය — deeper levels සඳහා වෙනස් shade එකක් අවශ්‍ය නැත.
7. Node label එකේ විශේෂ අකුරු (", (, ), {{, }}, |, :) තිබේ නම් label එක quotes
   ("...") තුළ දමන්න — උදා: A["සම්භවය (1948)"]:::branch1.
8. Syntax සම්පූර්ණයෙන්ම වලංගු (valid) බවට වගබලාගන්න.
9. **හැම විටම අවසානයේ, මේ පේළි 13ම හරියටම මෙසේම එකතු කරන්න** (වෙනස් නොකර):
   classDef root fill:#22c55e,color:#ffffff,stroke:#16a34a,stroke-width:3px,font-weight:bold
   classDef branch1 fill:#3b82f6,color:#ffffff,stroke:#2563eb,stroke-width:2px
   classDef branch1light fill:#bfdbfe,color:#1e3a5f,stroke:#3b82f6,stroke-width:1.5px
   classDef branch2 fill:#f97316,color:#ffffff,stroke:#ea580c,stroke-width:2px
   classDef branch2light fill:#fed7aa,color:#7c2d12,stroke:#f97316,stroke-width:1.5px
   classDef branch3 fill:#ec4899,color:#ffffff,stroke:#db2777,stroke-width:2px
   classDef branch3light fill:#fbcfe8,color:#831843,stroke:#ec4899,stroke-width:1.5px
   classDef branch4 fill:#a855f7,color:#ffffff,stroke:#9333ea,stroke-width:2px
   classDef branch4light fill:#e9d5ff,color:#581c87,stroke:#a855f7,stroke-width:1.5px
   classDef branch5 fill:#14b8a6,color:#ffffff,stroke:#0d9488,stroke-width:2px
   classDef branch5light fill:#99f6e4,color:#134e4a,stroke:#14b8a6,stroke-width:1.5px
   classDef branch6 fill:#ef4444,color:#ffffff,stroke:#dc2626,stroke-width:2px
   classDef branch6light fill:#fecaca,color:#7f1d1d,stroke:#ef4444,stroke-width:1.5px

උදාහරණයක් (structure එක විතරයි, content එක ඔබේම පාඩම අනුව):
flowchart TD
  Root[ප්‍රභාසංස්ලේෂණය]:::root --> A[කොන්දේසි]:::branch1
  A --> A1[සූර්ය ආලෝකය]:::branch1light
  A --> A2[ජලය]:::branch1light
  Root --> B[ක්‍රියාවලිය]:::branch2
  B --> B1["ග්ලූකෝස් (C6H12O6) නිපදවීම"]:::branch2light
  B --> B2[පත්‍ර වල සිදුවේ]:::branch2light
  Root --> C[ප්‍රතිඵල]:::branch3
  C --> C1[ඔක්සිජන් නිකුත් වීම]:::branch3light
  classDef root fill:#22c55e,color:#ffffff,stroke:#16a34a,stroke-width:3px,font-weight:bold
  classDef branch1 fill:#3b82f6,color:#ffffff,stroke:#2563eb,stroke-width:2px
  classDef branch1light fill:#bfdbfe,color:#1e3a5f,stroke:#3b82f6,stroke-width:1.5px
  classDef branch2 fill:#f97316,color:#ffffff,stroke:#ea580c,stroke-width:2px
  classDef branch2light fill:#fed7aa,color:#7c2d12,stroke:#f97316,stroke-width:1.5px
  classDef branch3 fill:#ec4899,color:#ffffff,stroke:#db2777,stroke-width:2px
  classDef branch3light fill:#fbcfe8,color:#831843,stroke:#ec4899,stroke-width:1.5px
  classDef branch4 fill:#a855f7,color:#ffffff,stroke:#9333ea,stroke-width:2px
  classDef branch4light fill:#e9d5ff,color:#581c87,stroke:#a855f7,stroke-width:1.5px
  classDef branch5 fill:#14b8a6,color:#ffffff,stroke:#0d9488,stroke-width:2px
  classDef branch5light fill:#99f6e4,color:#134e4a,stroke:#14b8a6,stroke-width:1.5px
  classDef branch6 fill:#ef4444,color:#ffffff,stroke:#dc2626,stroke-width:2px
  classDef branch6light fill:#fecaca,color:#7f1d1d,stroke:#ef4444,stroke-width:1.5px

JSON object එක parse කළ නොහැකි නම් සම්පූර්ණ පද්ධතියම අසාර්ථක වන බැවින්, ඉහත format
එකෙන් බැහැරව කිසිවක් නොදෙන්න.
"""


def _parse_json_loose(raw_text):
    text = raw_text.strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.MULTILINE).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        raise GeminiGenerationError("Gemini's response could not be parsed as JSON.")


def _sanitize_mermaid_labels(code):
    """FIX (Mermaid parse errors like 'Expecting SQE ... got PS'):
    Gemini is instructed to quote node labels containing special
    characters, but doesn't always do it reliably. Wraps EVERY
    square/round/circle-bracket node label in quotes unconditionally.

    FIX #2 (mindmap rendering broke entirely after this ran — "Syntax
    error in text"): the original character classes were [^()]+ /
    [^\\[\\]]+, which can match ACROSS newlines. If any single label
    anywhere in the code had an unquoted, unbalanced paren (e.g. Gemini
    wrote "Glucose (C6H12O6) production" without quotes), the regex
    would keep consuming forward hunting for a matching closing paren
    — swallowing subsequent lines and their indentation whitespace
    into one corrupted blob. Since mindmap syntax is 100% dependent on
    per-line indentation to express hierarchy, that corruption broke
    the whole diagram, not just the one bad label. Excluding \\n from
    every character class below means a match can never cross a line
    boundary, so one bad label now stays a contained, local problem.
    """
    if not code:
        return code

    def quote_square_label(match):
        node_id, label = match.group(1), match.group(2)
        stripped = label.strip()
        if stripped.startswith('"') and stripped.endswith('"'):
            return match.group(0)
        safe_label = stripped.replace('"', "'")
        return f'{node_id}["{safe_label}"]'

    def quote_round_label(match):
        node_id, label, suffix = match.group(1), match.group(2), match.group(3) or ''
        stripped = label.strip()
        if stripped.startswith('"') and stripped.endswith('"'):
            return match.group(0)
        safe_label = stripped.replace('"', "'")
        return f'{node_id}("{safe_label}"){suffix}'

    def quote_circle_label(match):
        node_id, label = match.group(1), match.group(2)
        stripped = label.strip()
        if stripped.startswith('"') and stripped.endswith('"'):
            return match.group(0)
        safe_label = stripped.replace('"', "'")
        return f'{node_id}(("{safe_label}"))'

    code = re.sub(r'([A-Za-z][A-Za-z0-9_]*)\(\(([^()\n]+)\)\)', quote_circle_label, code)
    code = re.sub(r'([A-Za-z][A-Za-z0-9_]*)\[([^\[\]\n]+)\]', quote_square_label, code)
    code = re.sub(r'([A-Za-z][A-Za-z0-9_]*)\(([^()\n]+)\)(:::\w+)?', quote_round_label, code)
    return code


# Maps Unicode subscript/superscript digits to plain ASCII digits.
_SUBSCRIPT_SUPERSCRIPT_MAP = str.maketrans({
    '₀': '0', '₁': '1', '₂': '2', '₃': '3', '₄': '4',
    '₅': '5', '₆': '6', '₇': '7', '₈': '8', '₉': '9',
    '⁰': '0', '¹': '1', '²': '2', '³': '3', '⁴': '4',
    '⁵': '5', '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9',
})


def _clean_mermaid_code(code):
    if not code or not isinstance(code, str):
        return ''
    code = code.strip()
    code = re.sub(r'^```(?:mermaid)?\s*|\s*```$', '', code, flags=re.MULTILINE).strip()
    code = code.translate(_SUBSCRIPT_SUPERSCRIPT_MAP)
    code = _sanitize_mermaid_labels(code)
    return code


def _clean_podcast_script(text):
    if not text or not isinstance(text, str):
        return text
    text = text.translate(_SUBSCRIPT_SUPERSCRIPT_MAP)

    def strip_latex_markup(match):
        inner = match.group(1)
        return re.sub(r'[_^{}\\]', '', inner)

    text = re.sub(r'\$([^$]+)\$', strip_latex_markup, text)
    text = re.sub(r'(?<=[A-Za-z0-9])_(?=[A-Za-z0-9])', '', text)
    text = re.sub(r'(?<=[A-Za-z0-9])\^(?=[A-Za-z0-9])', '', text)
    return text


def _is_transient_gemini_error(exc):
    msg = str(exc).upper()
    transient_markers = ['500', '502', '503', '504', 'INTERNAL', 'UNAVAILABLE', 'TIMEOUT', 'DEADLINE_EXCEEDED']
    return any(marker in msg for marker in transient_markers)


def _friendly_gemini_error_message(exc):
    """Turns a raw Gemini exception (often a big JSON error dump) into
    a short, clean message a student can actually understand, instead
    of showing them '503 UNAVAILABLE {...}' verbatim."""
    if _is_rate_limit_error(exc):
        return 'දැනට NoteWav AI එකේ ගොඩක් අය එකවර use කරනවා. තත්පර කිහිපයක් ඉඳලා නැවත උත්සාහ කරන්න, නැතහොත් "Full Text Mode" එකෙන් try කරන්න (AI අවශ්‍ය නැති, ක්ෂණික විකල්පයක්).'
    if _is_transient_gemini_error(exc):
        return 'NoteWav AI (Smart Study) servers දැනට busy වී ඇත (high demand). මිනිත්තුවක් විතර ඉඳලා නැවත උත්සාහ කරන්න, නැතහොත් "Full Text Mode" එකෙන් try කරන්න (AI අවශ්‍ය නැති, ක්ෂණික විකල්පයක්).'
    return 'AI processing එකේදී මොකක් හරි ගැටලුවක් ආවා. නැවත උත්සාහ කරන්න, නැතහොත් "Full Text Mode" එකෙන් try කරන්න.'


def _is_rate_limit_error(exc):
    msg = str(exc).upper()
    return '429' in msg or 'RESOURCE_EXHAUSTED' in msg or 'QUOTA' in msg


QUIZ_SYSTEM_INSTRUCTION = """
ඔබ ශ්‍රී ලංකාවේ අධ්‍යාපන AI සහායකයෙකි. ලබා දෙන පාඩම් content එකෙන්, MCQ (multiple choice
question) ප්‍රශ්න 5ක් සමන්විත quiz එකක් හදන්න. **JSON object එකක් විතරක්** output කරන්න,
වෙන කිසිදු text හෝ markdown fence නොදාන්න:

{
  "questions": [
    {
      "question": "<ප්‍රශ්නය>",
      "options": ["<option A>", "<option B>", "<option C>", "<option D>"],
      "correct_index": <0-3 අතර correct answer එකේ index එක>
    }
  ]
}

නීති:
- Content එකේ ලියැවී ඇති භාෂාවෙන්ම (Sinhala/Tamil/English) ප්‍රශ්න ලියන්න.
- ප්‍රශ්න content එකේ ඇති කරුණු පදනම් කරගෙන විය යුතුය (invented/false facts එපා).
- options 4 සියල්ලම reasonable විය යුතුය (obviously wrong options එපා), correct answer එකක්ම විය යුතුය.
- Questions 5ක්ම හදන්න, ඊට වඩා අඩු නොවේ.
"""


def call_gemini_quiz(content_text, max_retries=5):
    """Generates a 5-question multiple-choice quiz from note content,
    using the same Gemini client/retry pattern as the main script
    generation. Kept as a separate, lightweight function/prompt rather
    than folding into the main system instruction, since a quiz is an
    optional add-on most notes won't need."""
    if not client:
        raise GeminiGenerationError("Gemini API is not configured (missing GEMINI_API_KEY).")

    prompt = f"පහත content එකෙන් quiz එකක් හදන්න:\n\n{content_text}"

    last_error = None
    quiz_start_time = time.time()
    QUIZ_TIME_BUDGET_SECONDS = 45  # hard ceiling, safely under gunicorn's 120s worker timeout

    # Same automatic model fallback as call_gemini_structured — tries
    # the 3.x family first, falls back to the more established 2.5
    # family if 3.x is degraded.
    QUIZ_MODELS_TO_TRY = ['gemini-3.7-flash', 'gemini-2.5-flash']
    quiz_attempt_plan = [(model, n) for model in QUIZ_MODELS_TO_TRY for n in range(1, 2 + 1)]

    response = None
    for plan_index, (model_name, attempt) in enumerate(quiz_attempt_plan, start=1):
        # Same fix as call_gemini_structured — bounds each individual
        # call (http_options timeout) AND the whole function's total
        # time (budget check below), so a stalled network read can't
        # silently run past gunicorn's worker timeout and trigger a
        # SIGKILL.
        elapsed = time.time() - quiz_start_time
        if elapsed > QUIZ_TIME_BUDGET_SECONDS:
            raise GeminiGenerationError("Quiz generation timed out across retries (server-side time budget exceeded).")

        is_last_plan_step = (plan_index == len(quiz_attempt_plan))
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=QUIZ_SYSTEM_INSTRUCTION,
                    temperature=0.4,
                    max_output_tokens=2000,
                    response_mime_type='application/json',
                    safety_settings=SAFETY_SETTINGS,
                    http_options=types.HttpOptions(timeout=15_000),  # milliseconds
                )
            )
            break
        except Exception as e:
            last_error = e
            if not is_last_plan_step and _is_transient_gemini_error(e):
                time.sleep(2)
                continue
            if is_last_plan_step:
                raise GeminiGenerationError(f"Quiz generation failed after trying {QUIZ_MODELS_TO_TRY}: {last_error}")
            continue

    if response is None:
        raise GeminiGenerationError(f"Quiz generation failed after trying {QUIZ_MODELS_TO_TRY}: {last_error}")

    if not getattr(response, 'candidates', None):
        raise GeminiGenerationError("Quiz content was blocked by Gemini's safety filters.")

    raw_text = response.text
    if not raw_text:
        raise GeminiGenerationError("Gemini returned an empty quiz response.")

    data = _parse_json_loose(raw_text)
    questions = data.get('questions') or []
    if not questions:
        raise GeminiGenerationError("Gemini did not return any quiz questions.")

    # Log this call too — it hits the same shared Gemini quota.
    try:
        log_conn = get_db()
        log_conn.execute(
            "INSERT INTO gemini_calls (created_at) VALUES (?)",
            (datetime.now(timezone.utc).isoformat(),)
        )
        log_conn.commit()
        log_conn.close()
    except Exception as e:
        print(f"⚠️ Gemini call logging failed (non-critical): {e}")

    return questions


def call_gemini_structured(note_text, output_language='si', max_retries=3):
    if not client:
        raise GeminiGenerationError("Gemini API is not configured (missing GEMINI_API_KEY).")

    prompt = f"""පහත පාඩම් සටහන සකසන්න:

{note_text}
"""

    system_instruction = build_system_instruction(output_language)

    last_error = None
    synth_start_time = time.time()
    TOTAL_TIME_BUDGET_SECONDS = 45  # hard ceiling, safely under gunicorn's 120s worker timeout

    # NEW: automatic model fallback. Gemini's 3.x Flash family has been
    # showing repeated 503s/slowness recently (confirmed via external
    # status reports and Google's own rate-limit dashboard) — rather
    # than failing outright when the 3.x family is degraded, this now
    # also tries the older, more established 2.5 Flash family, which
    # often sits on separate capacity/infrastructure and may still be
    # healthy even when 3.x isn't. Each model gets 2 attempts; overall
    # time is still bounded by the same 45s budget below.
    MODELS_TO_TRY = ['gemini-3.7-flash', 'gemini-2.5-flash']
    ATTEMPTS_PER_MODEL = 2
    attempt_plan = [(model, n) for model in MODELS_TO_TRY for n in range(1, ATTEMPTS_PER_MODEL + 1)]

    response = None
    for plan_index, (model_name, attempt) in enumerate(attempt_plan, start=1):
        # FIX (root cause of the "very slow / SIGKILL" incident):
        # the previous fix here only accounted for SLEEP time between
        # retries — it never bounded how long any INDIVIDUAL API call
        # itself could take. A single stalled network read (Gemini's
        # side going quiet mid-response) could hang indefinitely,
        # completely unaffected by how conservative the retry/backoff
        # numbers were. That hang eventually ran past gunicorn's own
        # 120s worker timeout, forcing gunicorn to SIGKILL the whole
        # worker process — which is exactly the crash the user hit.
        # Two fixes, matching the pattern already used for TTS:
        # (1) an explicit per-call network timeout via http_options,
        # and (2) a hard TOTAL time budget across the whole function
        # (all models + all attempts + all sleeps combined), so
        # nothing can ever add up to gunicorn's limit.
        elapsed = time.time() - synth_start_time
        if elapsed > TOTAL_TIME_BUDGET_SECONDS:
            print(f"⚠️ call_gemini_structured aborting — total time budget ({TOTAL_TIME_BUDGET_SECONDS}s) exceeded after {elapsed:.1f}s")
            raise GeminiGenerationError("Gemini request timed out across retries (server-side time budget exceeded). නැවත උත්සාහ කරන්න.")

        is_last_plan_step = (plan_index == len(attempt_plan))
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.3,
                    max_output_tokens=8000,
                    response_mime_type='application/json',
                    safety_settings=SAFETY_SETTINGS,
                    http_options=types.HttpOptions(timeout=15_000),  # milliseconds
                )
            )
            print(f"✅ Gemini succeeded using {model_name} (attempt {attempt})")
            break  # success — exit the retry loop
        except Exception as e:
            last_error = e

            if _is_rate_limit_error(e):
                if not is_last_plan_step:
                    print(f"⚠️ {model_name} rate-limited, moving to next attempt/model: {e}")
                    continue
                raise GeminiGenerationError(
                    "Gemini API එකේ දෛනික/විනාඩි quota එක ඉවර වී ඇත (free tier limit). "
                    "පැය කිහිපයකින් හෝ මිනිත්තු කිහිපයකින් නැවත උත්සාහ කරන්න, නැතහොත් "
                    "Gemini API එකේ paid billing plan එකකට upgrade කරන්න."
                )

            if not is_last_plan_step and _is_transient_gemini_error(e):
                # FIX: an earlier version of this bumped retries up to
                # 8 attempts with backoff up to 12s, aiming to survive
                # longer "high demand" spikes. But the WORST-CASE total
                # time (sleep + actual API call time across all
                # attempts) came dangerously close to gunicorn's 120s
                # worker timeout. Kept short here (2s/model-switch) —
                # trying the OTHER model matters more than waiting
                # longer on the same struggling one.
                wait_seconds = 2
                print(f"⚠️ {model_name} transient error (attempt {attempt}/{ATTEMPTS_PER_MODEL}), retrying/switching in {wait_seconds}s: {e}")
                time.sleep(wait_seconds)
                continue
            if is_last_plan_step:
                raise GeminiGenerationError(f"Gemini request failed after trying {MODELS_TO_TRY}: {last_error}")
            # Non-transient error on a non-final step — still worth
            # trying the next model/attempt rather than giving up.
            continue

    if response is None:
        raise GeminiGenerationError(f"Gemini request failed after trying {MODELS_TO_TRY}: {last_error}")

    if not getattr(response, 'candidates', None):
        block_reason = getattr(getattr(response, 'prompt_feedback', None), 'block_reason', 'unknown')
        raise GeminiGenerationError(f"Content was blocked by Gemini's safety filters ({block_reason}).")

    candidate = response.candidates[0]
    finish_reason = str(getattr(candidate, 'finish_reason', '') or '')
    if 'SAFETY' in finish_reason.upper():
        raise GeminiGenerationError("Content was blocked by Gemini's safety filters.")
    if 'MAX_TOKENS' in finish_reason.upper():
        raise GeminiGenerationError(
            "Gemini's response was cut off (hit the output length limit) — try shortening the note a bit."
        )

    raw_text = response.text
    if not raw_text:
        raise GeminiGenerationError("Gemini returned an empty response.")

    data = _parse_json_loose(raw_text)

    podcast_script = _clean_podcast_script((data.get('podcast_script') or '').strip())
    if '$' in podcast_script or '_' in podcast_script:
        print(f"⚠️ podcast_script still contains $ or _ after cleanup: {podcast_script[:200]!r}")
    mermaid_code_si = _clean_mermaid_code(data.get('mermaid_code_si'))
    mermaid_code_en = _clean_mermaid_code(data.get('mermaid_code_en'))

    if not podcast_script:
        raise GeminiGenerationError("Gemini did not return a podcast script.")

    # Log this successful call for the admin's monthly usage tracker.
    # Logging failure must NEVER break the actual feature, so it's
    # wrapped in its own try/except and silently ignored on error.
    try:
        log_conn = get_db()
        log_conn.execute(
            "INSERT INTO gemini_calls (created_at) VALUES (?)",
            (datetime.now(timezone.utc).isoformat(),)
        )
        log_conn.commit()
        log_conn.close()
    except Exception as e:
        print(f"⚠️ Gemini call logging failed (non-critical): {e}")

    return podcast_script, mermaid_code_si, mermaid_code_en


@app.route('/pdf-extract', methods=['POST'])
@rate_limited(6, 60)
def pdf_extract():
    """Renders each PDF page as an IMAGE and runs it through the same
    Cloud Vision OCR used for photos, instead of reading the PDF's text
    layer directly.

    WHY: many Sri Lankan PDFs are typed using legacy non-Unicode Sinhala
    fonts (FM Abhaya, Kaputa, DL-Manel, etc.) — these fonts make Sinhala
    glyphs LOOK correct on screen, but the underlying stored character
    codes are actually plain Latin/ASCII, mapped to Sinhala shapes only
    by that specific font. Reading the text layer directly (as pypdf
    did before) returns those raw underlying codes — garbled nonsense
    like "fjk;a lsishï m%;spdrhla". Rendering the page as an image and
    reading it visually via OCR sidesteps this entirely, exactly like
    photographing the page would — it doesn't matter what font or
    encoding was used, only what the text visually looks like."""
    if 'pdf' not in request.files:
        return jsonify({'success': False, 'error': 'No PDF uploaded'}), 400
    if not CLOUD_OCR_AVAILABLE:
        return jsonify({'success': False, 'error': 'Cloud Vision API not configured'}), 500

    file = request.files['pdf']
    content = file.read()

    if len(content) > 8 * 1024 * 1024:
        return jsonify({'success': False, 'message': 'PDF ගොනුව ඉතා විශාලයි (උපරිම 8MB).'}), 400

    try:
        doc = fitz.open(stream=content, filetype='pdf')
        MAX_PAGES = 10  # cost/time safety cap for very long PDFs
        page_count = min(len(doc), MAX_PAGES)
        pages_text = []

        for i in range(page_count):
            page = doc[i]
            # matrix=2x2 renders at roughly double the PDF's native
            # resolution (~144 DPI) — sharp enough for reliable OCR
            # without making the image unnecessarily huge/slow.
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            img_bytes = pix.tobytes('png')

            image = vision.Image(content=img_bytes)
            image_context = vision.ImageContext(language_hints=['si', 'ta', 'en'])
            response = vision_client.text_detection(image=image, image_context=image_context)

            if response.error.message:
                print(f"⚠️ Vision error on PDF page {i + 1}: {response.error.message}")
                continue

            texts = response.text_annotations
            if texts:
                page_text = texts[0].description.strip()
                if page_text:
                    pages_text.append(f"--- Page {i + 1} ---\n{page_text}")

        total_pages_in_pdf = len(doc)  # capture BEFORE closing — len(doc) after close() raises "document closed"
        doc.close()
        full_text = '\n\n'.join(pages_text).strip()

        if len(full_text) < 3:
            return jsonify({
                'success': False,
                'text': '',
                'message': 'මේ PDF එකෙන් පෙළ උපුටාගැනීමට නොහැකි විය. පැහැදිලි pages සහිත PDF එකක් උත්සාහ කරන්න.'
            })

        note = None
        if total_pages_in_pdf > MAX_PAGES:
            note = f'PDF එකේ pages {total_pages_in_pdf}ක් තිබුණි — safety හේතුවෙන් මුල් pages {MAX_PAGES} විතරක් process කරන ලදී.'

        return jsonify({
            'success': True,
            'text': full_text,
            'length': len(full_text),
            'pages': page_count,
            'note': note,
        })
    except Exception as e:
        print(f"❌ PDF extract error: {e}")
        error_detail = f'PDF එක process කිරීමට නොහැකි විය: {e}'
        return jsonify({'success': False, 'error': error_detail, 'message': error_detail}), 500


@app.route('/ocr', methods=['POST'])
@rate_limited(10, 60)  # 10 OCR calls per minute per device — Google Cloud Vision has its own free quota
def ocr_image():
    print("=" * 50)
    print("📸 OCR Request Received")

    if not CLOUD_OCR_AVAILABLE:
        print("❌ Cloud Vision API not configured")
        return jsonify({'success': False, 'error': 'Cloud Vision API not configured'}), 500

    if 'image' not in request.files:
        print("❌ No image in request")
        return jsonify({'success': False, 'error': 'No image uploaded'}), 400

    try:
        file = request.files['image']
        content = file.read()

        print(f"📸 Image size: {len(content)} bytes")
        print(f"📸 Image type: {file.content_type}")

        if len(content) < 100:
            print("❌ Image too small!")
            return jsonify({
                'success': False,
                'text': '',
                'message': 'රූපය ඉතා කුඩායි. පැහැදිලි රූපයක් උත්සාහ කරන්න.'
            })

        image = vision.Image(content=content)
        image_context = vision.ImageContext(language_hints=['si', 'ta', 'en'])

        print("🔍 Calling Vision API...")
        response = vision_client.text_detection(image=image, image_context=image_context)

        if response.error.message:
            print(f"❌ Vision API Error: {response.error.message}")
            return jsonify({'success': False, 'error': response.error.message}), 500

        texts = response.text_annotations
        print(f"📝 Found {len(texts)} text annotations")

        if texts:
            extracted_text = texts[0].description.strip()
            print(f"📝 Extracted text length: {len(extracted_text)}")

            if len(extracted_text) < 3:
                return jsonify({
                    'success': False,
                    'text': '',
                    'message': 'පෙළ හඳුනා ගැනීම අසාර්ථක විය. පැහැදිලි රූපයක් උත්සාහ කරන්න.'
                })

            detected_lang = detect_language(extracted_text)
            print(f"🌐 Detected language: {detected_lang}")
            print("✅ OCR Success!")
            print("=" * 50)

            return jsonify({
                'success': True,
                'text': extracted_text,
                'length': len(extracted_text),
                'detected_language': detected_lang
            })
        else:
            print("❌ No text annotations found")
            print("=" * 50)
            return jsonify({
                'success': False,
                'text': '',
                'message': 'පෙළක් හමු නොවීය. පැහැදිලි රූපයක් උත්සාහ කරන්න.'
            })

    except Exception as e:
        print(f"❌ OCR Exception: {str(e)}")
        import traceback
        traceback.print_exc()
        print("=" * 50)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/tts', methods=['POST'])
@rate_limited(10, 60)  # 10 audio generations per minute per device
def text_to_speech():
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    # NEW: block banned devices/accounts from generating audio.
    if _is_banned(anon_id=(data.get('anon_id') or '').strip()[:64], email=session.get('user_email')):
        return jsonify({'status': 'error', 'message': 'ඔබේ account/device එකට මේ feature එක restrict කර ඇත.'}), 403
    # NEW: which voice engine to use — 'gtts' (default, free, classic
    # robotic TTS) or 'gemini' (natural LLM-driven voice, Preview,
    # costs Gemini API usage). Anything unrecognized falls back to gtts.
    engine = (data.get('engine') or 'gtts').strip().lower()
    if engine not in ('gtts', 'gemini'):
        engine = 'gtts'
    voice_name = (data.get('voice_name') or '').strip()
    model_version = (data.get('model_version') or 'v25').strip()
    if model_version not in GEMINI_TTS_MODEL_VERSIONS:
        model_version = 'v25'

    if not text:
        return jsonify({'status': 'error', 'message': 'කියවීමට කිසිම text එකක් ලැබී නැත.'}), 400

    length_error = validate_text_length(text)
    if length_error:
        return jsonify({'status': 'error', 'message': length_error}), 400

    formatted_text = format_for_podcast(text)
    if len(formatted_text) < 10:
        return jsonify({'status': 'error', 'message': 'Audio හදන්න text එක ඉතා කෙටියි.'}), 400

    if not os.path.exists('static'):
        os.makedirs('static')
    cleanup_old_audio()

    detected_lang = detect_language(formatted_text)
    gtts_lang = {'Sinhala': 'si', 'Tamil': 'ta', 'English': 'en'}.get(detected_lang, 'si')

    if engine == 'gemini':
        try:
            print(f"🎙️ Requesting Gemini TTS ({len(formatted_text)} chars, lang: {gtts_lang})...")
            audio_segment, sentence_timings = synthesize_gemini_tts(formatted_text, lang=gtts_lang, voice_name=voice_name, model_version=model_version)
            unique_name = f"output_{uuid.uuid4().hex}.mp3"
            filename = os.path.join('static', unique_name)
            audio_segment.export(filename, format='mp3')
            print("✅ Gemini TTS Success!")
            return jsonify({
                'status': 'success',
                'audio_url': '/' + filename.replace('\\', '/'),
                'sentence_timings': sentence_timings,
                'engine': 'gemini',
                'coin_cost': calculate_gemini_tts_coin_cost(len(formatted_text), model_version=model_version),
                'model_version': model_version,
            })
        except Exception as e:
            print(f"❌ Gemini TTS Error: {str(e)}")
            return jsonify({
                'status': 'error',
                'message': 'NoteWav AI Voice (Beta) එකෙන් audio එක generate කරගැනීම අසාර්ථක විය — Standard Voice එකෙන් try කරන්න.'
            }), 500

    try:
        print(f"🎙️ Requesting gTTS ({len(formatted_text)} chars, lang: {gtts_lang})...")
        audio_segment, sentence_timings = synthesize_gtts_natural(formatted_text, lang=gtts_lang)
        unique_name = f"output_{uuid.uuid4().hex}.mp3"
        filename = os.path.join('static', unique_name)
        audio_segment.export(filename, format='mp3')
        print("✅ TTS Success!")
        return jsonify({
            'status': 'success',
            'audio_url': '/' + filename.replace('\\', '/'),
            'sentence_timings': sentence_timings,
            'engine': 'gtts',
        })
    except Exception as e:
        print(f"❌ gTTS Error: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': 'gTTS හරහා audio එක generate කරගැනීම අසාර්ථක විය. පසුව උත්සාහ කරන්න.'
        }), 500


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/.well-known/assetlinks.json')
def assetlinks_json():
    """Digital Asset Links verification for the TWA (Trusted Web
    Activity) Android package built via PWABuilder — proves this
    website controls/authorizes that specific APK (identified by its
    package name + SHA-256 signing certificate fingerprint). Without
    this, Android can't verify the installed app owns this domain, so
    it falls back to showing a browser-style URL bar inside the app
    instead of a clean fullscreen experience.

    NOTE: PWABuilder generates a NEW random signing key every time you
    regenerate the Android package, UNLESS you explicitly download and
    reuse the same keystore file it offers on the package page. That's
    why the fingerprint changed between builds — going forward, reuse
    the same keystore to avoid needing to update this list again."""
    content = [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": "com.onrender.notewav.twa",
            "sha256_cert_fingerprints": [
                "A4:FD:29:75:B6:5F:ED:A9:61:CE:59:CD:0F:C0:17:8B:2F:20:A7:8D:90:DA:AA:E7:A8:90:6F:D3:95:5D:F0:1D",
                "74:32:96:EF:A0:28:FD:CA:AC:63:F2:44:1C:42:80:0D:8D:66:C6:C3:F7:4C:64:CA:02:D8:6C:E9:02:A4:E5:10",
                "16:EB:70:C9:42:C3:A1:32:8C:23:47:05:B8:3D:9B:F9:6C:40:B9:0B:01:2F:EA:70:64:41:B2:34:B3:0A:FB:57"
            ]
        }
    }]
    return jsonify(content)


@app.route('/ads.txt')
def ads_txt():
    """Required by Google AdSense to verify this site is authorized to
    show ads from this publisher account. The 'DIRECT' relationship and
    the fixed certification ID (f08c47fec0942fa0) are Google's own
    standard values for AdSense — same for every publisher, only the
    pub- ID changes."""
    content = "google.com, pub-4882546078529900, DIRECT, f08c47fec0942fa0\n"
    return app.response_class(content, mimetype='text/plain')


@app.route('/privacy')
def privacy_page():
    return render_template('privacy.html')


@app.route('/terms')
def terms_page():
    return render_template('terms.html')


# ========================================
# GOOGLE SIGN-IN ROUTES
# ========================================
@app.route('/auth/google/login')
def google_login():
    if not GOOGLE_LOGIN_CONFIGURED:
        return "Google Sign-In is not configured on this server yet.", 500
    # A random per-session token, checked again in the callback, so a
    # forged/replayed callback request can't be used to log someone
    # into an account they didn't actually authorize (CSRF protection
    # for the OAuth flow).
    state = secrets.token_urlsafe(24)
    session['oauth_state'] = state
    params = {
        'client_id': GOOGLE_CLIENT_ID,
        'redirect_uri': GOOGLE_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
        'prompt': 'select_account',
    }
    return redirect(f'{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}')


@app.route('/auth/google/callback')
def google_callback():
    if request.args.get('error'):
        return redirect(url_for('home'))

    state = request.args.get('state')
    if not state or state != session.get('oauth_state'):
        return "Invalid or expired login attempt — please try signing in again.", 400
    session.pop('oauth_state', None)

    code = request.args.get('code')
    if not code:
        return redirect(url_for('home'))

    try:
        token_response = requests.post(GOOGLE_TOKEN_ENDPOINT, data={
            'code': code,
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'redirect_uri': GOOGLE_REDIRECT_URI,
            'grant_type': 'authorization_code',
        }, timeout=10)
        token_response.raise_for_status()
        access_token = token_response.json().get('access_token')

        userinfo_response = requests.get(
            GOOGLE_USERINFO_ENDPOINT,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10,
        )
        userinfo_response.raise_for_status()
        profile = userinfo_response.json()
    except Exception as e:
        print(f"❌ Google OAuth exchange failed: {e}")
        return "Google login failed — please try again.", 500

    google_id = profile.get('sub')
    email = profile.get('email', '')
    name = profile.get('name', '')
    picture = profile.get('picture', '')
    if not google_id:
        return "Google login failed — no account ID returned.", 500

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    existing = conn.execute("SELECT * FROM users WHERE google_id = ?", (google_id,)).fetchone()
    if existing:
        # NEW: name is NOT overwritten here anymore. If the person has
        # customized their display name (via the edit-name feature),
        # that choice should survive every future Google login — only
        # email/picture/last_login refresh automatically. Name only
        # ever gets its initial value from Google at account CREATION
        # (the 'else' branch below), and after that, only the
        # dedicated /user/update-profile endpoint can change it.
        conn.execute(
            "UPDATE users SET email = ?, picture = ?, last_login = ? WHERE google_id = ?",
            (email, picture, now_iso, google_id)
        )
    else:
        # New account — starts with the same 100 free coins guests get,
        # and a fresh streak.
        conn.execute(
            """INSERT INTO users (google_id, email, name, picture, coins, streak, last_streak_date, created_at, last_login)
               VALUES (?, ?, ?, ?, 100, 0, NULL, ?, ?)""",
            (google_id, email, name, picture, now_iso, now_iso)
        )
    conn.commit()
    conn.close()

    session.permanent = True  # survives closing/reopening the app, not just a single browser session
    session['user_id'] = google_id
    session['user_name'] = name
    session['user_email'] = email
    session['user_picture'] = picture
    return redirect(url_for('home'))


@app.route('/auth/logout')
def auth_logout():
    session.pop('user_id', None)
    session.pop('user_name', None)
    session.pop('user_email', None)
    session.pop('user_picture', None)
    return redirect(url_for('home'))


@app.route('/auth/me')
def auth_me():
    """Tells the frontend whether the visitor is signed in, and if so,
    hands back their server-side coins/streak/profile — the single
    source of truth once someone has an account, so every device they
    log into shows the same numbers."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'logged_in': False, 'google_login_available': GOOGLE_LOGIN_CONFIGURED})
    try:
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE google_id = ?", (user_id,)).fetchone()
        conn.close()
        if not row:
            session.pop('user_id', None)
            return jsonify({'logged_in': False, 'google_login_available': GOOGLE_LOGIN_CONFIGURED})
        return jsonify({
            'logged_in': True,
            'name': row['name'],
            'email': row['email'],
            'picture': row['picture'],
            'coins': row['coins'],
            'streak': row['streak'],
            'last_streak_date': row['last_streak_date'],
        })
    except Exception as e:
        print(f"❌ /auth/me error: {e}")
        return jsonify({'logged_in': False, 'google_login_available': GOOGLE_LOGIN_CONFIGURED})


@app.route('/user/update-profile', methods=['POST'])
def user_update_profile():
    """Lets a signed-in person set a custom display name on their
    account — persisted server-side so it follows them to every device
    they log into (unlike the old behavior where Google's own profile
    name would silently overwrite it on each login)."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'ignored'})
    try:
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()[:40]
        if not name:
            return jsonify({'status': 'error', 'message': 'Name එකක් ලියන්න.'}), 400
        conn = get_db()
        conn.execute("UPDATE users SET name = ? WHERE google_id = ?", (name, user_id))
        conn.commit()
        conn.close()
        session['user_name'] = name
        return jsonify({'status': 'success', 'name': name})
    except Exception as e:
        print(f"⚠️ /user/update-profile failed: {e}")
        return jsonify({'status': 'error'}), 200
@app.route('/user/sync', methods=['POST'])
def user_sync():
    """Lets the client push updated coins/streak values back up to the
    signed-in user's account record, so they stay in sync everywhere.
    Guests (not logged in) get 'ignored' here — their coins/streak
    remain device-local only, exactly as before Google Sign-In existed."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'ignored'})
    try:
        data = request.get_json(silent=True) or {}
        updates, params = [], []
        if isinstance(data.get('coins'), int):
            updates.append('coins = ?')
            params.append(data['coins'])
        if isinstance(data.get('streak'), int):
            updates.append('streak = ?')
            params.append(data['streak'])
        if isinstance(data.get('last_streak_date'), str):
            updates.append('last_streak_date = ?')
            params.append(data['last_streak_date'])
        if updates:
            params.append(user_id)
            conn = get_db()
            conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE google_id = ?", params)
            conn.commit()
            conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        print(f"⚠️ /user/sync failed (non-critical): {e}")
        return jsonify({'status': 'error'}), 200


# ========================================
# BUG FIX: Service Worker root-scope route
# ========================================
# The service worker file physically lives at static/sw.js, and it was
# being registered from script.js as navigator.serviceWorker.register
# ('/static/sw.js'). Registering a service worker from a URL under
# /static/ automatically restricts its "scope" to /static/ — meaning it
# can NEVER control the home page ("/"), only files under /static/.
#
# Several browsers (Samsung Internet in particular) require the
# service worker to control start_url ("/", per manifest.json) as part
# of their install-prompt eligibility checks. Because the scope was
# wrongly limited to /static/, that check silently failed and the
# "Install app" button never appeared, even though everything else
# (manifest.json, icons, HTTPS) was correct.
#
# Fix: serve the exact same sw.js file content from the ROOT path
# (/sw.js) instead. script.js's register() call is updated (see
# script.js) to register '/sw.js' with an explicit scope of '/', which
# gives the service worker control over the entire site as intended.
@app.route('/sw.js')
def service_worker():
    response = send_from_directory('static', 'sw.js')
    # Explicitly declare the widest allowed scope in the response
    # header too — belt-and-suspenders alongside the register({scope})
    # call on the frontend, since some browsers check this header.
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Content-Type'] = 'application/javascript'
    return response


@app.route('/generate-quiz', methods=['POST'])
@rate_limited(6, 60)
def generate_quiz():
    data = request.get_json(silent=True) or {}
    content_text = (data.get('text') or '').strip()
    if not content_text:
        return jsonify({'status': 'error', 'message': 'Quiz එකක් හදන්න content එකක් නෑ.'}), 400

    length_error = validate_text_length(content_text)
    if length_error:
        return jsonify({'status': 'error', 'message': length_error}), 400

    try:
        questions = call_gemini_quiz(content_text)
    except GeminiGenerationError as e:
        print(f"Quiz Gemini Error: {e}")
        return jsonify({'status': 'error', 'message': _friendly_gemini_error_message(e)}), 500

    return jsonify({'status': 'success', 'questions': questions})


@app.route('/process-note', methods=['POST'])
@rate_limited(8, 60)  # 8 Gemini calls per minute per device — protects the shared free API quota
def process_note():
    data = request.get_json(silent=True) or {}
    note_text = (data.get('text') or '').strip()
    mode = data.get('mode', 'full')
    # NEW: block banned devices/accounts from processing notes.
    if _is_banned(anon_id=(data.get('anon_id') or '').strip()[:64], email=session.get('user_email')):
        return jsonify({'status': 'error', 'message': 'ඔබේ account/device එකට මේ feature එක restrict කර ඇත.'}), 403
    # NEW: the person now explicitly chooses the OUTPUT language for the
    # podcast script via a toggle in the UI ('si' or 'en') — independent
    # of whatever language the note itself is written in. Defaults to
    # 'si' if not provided (e.g. an older cached frontend).
    output_language = data.get('output_language', 'si')
    if output_language not in ('si', 'en'):
        output_language = 'si'

    if not note_text:
        return jsonify({'status': 'error', 'message': 'කරුණාකර පාඩම් සටහනක් ඇතුළත් කරන්න.'}), 400

    length_error = validate_text_length(note_text)
    if length_error:
        return jsonify({'status': 'error', 'message': length_error}), 400

    if not client:
        return jsonify({
            'status': 'success',
            'processed_text': note_text,
            'mermaid_code_si': '',
            'mermaid_code_en': '',
            'ai_processed': False,
            'warning': 'NoteWav AI configure වී නොමැති බැවින් Smart Study සහ Mind Map ලබාගත නොහැක.'
        })

    # FIX (emergency reliability): Full Text Mode was supposed to be
    # the fast, no-AI-dependency option, but this call to Gemini ran
    # UNCONDITIONALLY regardless of mode — the mode check only decided
    # which TEXT to use afterward (line below), not whether to call
    # Gemini at all. That meant Full Text Mode was JUST AS exposed to
    # Gemini being slow/degraded as Smart Study mode, even though the
    # UI (and earlier advice) implied it wasn't. Now genuinely skips
    # Gemini for 'full' mode — instant, reliable, no mind map for this
    # mode until Gemini's current issues settle down.
    if mode != 'smart':
        return jsonify({
            'status': 'success',
            'processed_text': note_text,
            'mermaid_code_si': '',
            'mermaid_code_en': '',
            'ai_processed': False,
            'warning': ''
        })

    try:
        podcast_script, mermaid_code_si, mermaid_code_en = call_gemini_structured(note_text, output_language)
    except GeminiGenerationError as e:
        print(f"Gemini Error: {e}")
        return jsonify({
            'status': 'success',
            'processed_text': note_text,
            'mermaid_code_si': '',
            'mermaid_code_en': '',
            'ai_processed': False,
            'warning': _friendly_gemini_error_message(e)
        })

    final_text = podcast_script if mode == 'smart' else note_text

    return jsonify({
        'status': 'success',
        'processed_text': final_text,
        'mermaid_code_si': mermaid_code_si,
        'mermaid_code_en': mermaid_code_en,
        'ai_processed': True
    })


@app.route('/library/save', methods=['POST'])
def library_save():
    # NEW: saving to the Library now requires being signed in with
    # Google — this is a deliberate change from the earlier
    # guest-friendly behavior. Enforced here server-side (in addition
    # to a friendly prompt on the frontend) so this can't be bypassed.
    if not session.get('user_id'):
        return jsonify({
            'status': 'error',
            'message': 'Library එකට save කරන්න Google account එකකින් login වෙන්න ඕන.',
            'login_required': True,
        }), 401

    data = request.get_json(silent=True) or {}
    subject = (data.get('subject') or 'General').strip()[:80] or 'General'
    note_text = (data.get('note_text') or '').strip()
    processed_text = data.get('processed_text') or ''
    mermaid_code_si = data.get('mermaid_code_si') or ''
    mermaid_code_en = data.get('mermaid_code_en') or ''
    mode = data.get('mode') or 'full'
    anon_id = (data.get('anon_id') or '').strip()[:64]
    user_name = (data.get('user_name') or '').strip()[:80]

    if not note_text:
        return jsonify({'status': 'error', 'message': 'Save කරන්න note එකක් නෑ.'}), 400

    length_error = validate_text_length(note_text)
    if length_error:
        return jsonify({'status': 'error', 'message': length_error}), 400

    title_source = (processed_text or note_text).strip()
    title = ' '.join(title_source.split()[:8])
    if len(title) < len(title_source):
        title += '...'
    if not title:
        title = 'Untitled Note'

    # If signed in via Google, tag this note as belonging to that
    # account (private + synced across their devices). Guests (no
    # session) leave this NULL — their notes stay in the shared/global
    # Library exactly as before, no behavior change for them.
    owner_google_id = session.get('user_id')

    # NEW: optional original photo (base64 data URL, already compressed
    # client-side) — capped generously but firmly, so one oversized
    # image can't bloat the database or blow past Turso's per-row size
    # comfort zone. If it's over the cap, we just skip saving the image
    # (the note itself still saves fine) rather than failing the whole
    # request over an image that's too big.
    source_image = data.get('source_image') or None
    MAX_SOURCE_IMAGE_CHARS = 700_000  # ~500KB of actual image data, base64-inflated
    if source_image and len(source_image) > MAX_SOURCE_IMAGE_CHARS:
        print(f"⚠️ source_image too large ({len(source_image)} chars) — skipping, saving note without it.")
        source_image = None

    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO notes
               (subject, title, note_text, processed_text, mermaid_code_si, mermaid_code_en, mode, created_at, anon_id, user_name, owner_google_id, source_image_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subject, title, note_text, processed_text, mermaid_code_si, mermaid_code_en, mode,
             datetime.now(timezone.utc).isoformat(), anon_id, user_name, owner_google_id, source_image)
        )
        conn.commit()
        new_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        return jsonify({'status': 'success', 'id': new_id, 'title': title, 'subject': subject})
    except Exception as e:
        print(f"❌ Library save error: {e}")
        return jsonify({'status': 'error', 'message': 'Note එක save කරගැනීම අසාර්ථක විය.'}), 500


@app.route('/library/notes', methods=['GET'])
def library_list():
    try:
        conn = get_db()
        user_id = session.get('user_id')
        # NEW: optional full-text search — matches title/subject AND the
        # actual saved note content (note_text/processed_text), not just
        # title/subject like before. Empty/missing q returns everything,
        # unchanged from prior behavior.
        query = (request.args.get('q') or '').strip()
        like_term = f'%{query}%'

        if user_id:
            # Signed in: PRIVATE list — only this account's own notes,
            # so it's the same list on every device they log into.
            if query:
                rows = conn.execute(
                    """SELECT id, subject, title, mode, created_at, (source_image_data IS NOT NULL) AS has_image FROM notes
                       WHERE owner_google_id = ?
                       AND (title LIKE ? OR subject LIKE ? OR note_text LIKE ? OR processed_text LIKE ?)
                       ORDER BY created_at DESC""",
                    (user_id, like_term, like_term, like_term, like_term)
                ).fetchall()
            else:
                rows = conn.execute(
                    'SELECT id, subject, title, mode, created_at, (source_image_data IS NOT NULL) AS has_image FROM notes WHERE owner_google_id = ? ORDER BY created_at DESC',
                    (user_id,)
                ).fetchall()
        else:
            # Guest: only notes with NO account owner (true guest-saved
            # notes) — a signed-in account's private notes must NEVER
            # be visible here.
            if query:
                rows = conn.execute(
                    """SELECT id, subject, title, mode, created_at, (source_image_data IS NOT NULL) AS has_image FROM notes
                       WHERE owner_google_id IS NULL
                       AND (title LIKE ? OR subject LIKE ? OR note_text LIKE ? OR processed_text LIKE ?)
                       ORDER BY created_at DESC""",
                    (like_term, like_term, like_term, like_term)
                ).fetchall()
            else:
                rows = conn.execute(
                    'SELECT id, subject, title, mode, created_at, (source_image_data IS NOT NULL) AS has_image FROM notes WHERE owner_google_id IS NULL ORDER BY created_at DESC'
                ).fetchall()
        conn.close()
        notes = [dict(row) for row in rows]
        return jsonify({'status': 'success', 'notes': notes})
    except Exception as e:
        print(f"❌ Library list error: {e}")
        return jsonify({'status': 'error', 'message': 'Library එක load කරගැනීම අසාර්ථක විය.'}), 500


@app.route('/library/export', methods=['GET'])
def library_export():
    try:
        conn = get_db()
        rows = conn.execute(
            """SELECT subject, title, note_text, processed_text, mermaid_code_si,
                      mermaid_code_en, mode, created_at FROM notes ORDER BY created_at DESC"""
        ).fetchall()
        conn.close()
        notes = [dict(row) for row in rows]
        return jsonify({'status': 'success', 'notes': notes, 'exported_at': datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        print(f"❌ Library export error: {e}")
        return jsonify({'status': 'error', 'message': 'Export කිරීම අසාර්ථක විය.'}), 500


@app.route('/library/notes/<int:note_id>', methods=['GET'])
def library_get(note_id):
    try:
        conn = get_db()
        row = conn.execute('SELECT * FROM notes WHERE id = ?', (note_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({'status': 'error', 'message': 'Note එක හමු නොවීය.'}), 404
        return jsonify({'status': 'success', 'note': dict(row)})
    except Exception as e:
        print(f"❌ Library get error: {e}")
        return jsonify({'status': 'error', 'message': 'Note එක load කරගැනීම අසාර්ථක විය.'}), 500


@app.route('/library/notes/<int:note_id>', methods=['DELETE'])
def library_delete(note_id):
    try:
        conn = get_db()
        # If this note is privately owned (a signed-in account's note),
        # only THAT account may delete it. Guest notes (owner_google_id
        # is NULL) keep the original no-restriction behavior.
        row = conn.execute('SELECT owner_google_id FROM notes WHERE id = ?', (note_id,)).fetchone()
        if row and row['owner_google_id'] and row['owner_google_id'] != session.get('user_id'):
            conn.close()
            return jsonify({'status': 'error', 'message': 'ඔබට මේ note එක delete කරන්න අවසර නෑ.'}), 403
        conn.execute('DELETE FROM notes WHERE id = ?', (note_id,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        print(f"❌ Library delete error: {e}")
        return jsonify({'status': 'error', 'message': 'Note එක delete කරගැනීම අසාර්ථක විය.'}), 500


@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'cloud_ocr': CLOUD_OCR_AVAILABLE,
        'gemini': bool(client),
        'gtts_tts': GTTS_AVAILABLE,
        'library': os.path.exists(DB_PATH),
        'max_text_length': MAX_TEXT_LENGTH,
    })


# ========================================
# LIGHTWEIGHT USAGE TRACKING (for the admin dashboard)
# ========================================
@app.route('/track', methods=['POST'])
def track_event():
    try:
        data = request.get_json(silent=True) or {}
        anon_id = (data.get('anon_id') or '').strip()[:64]
        user_name = (data.get('user_name') or '').strip()[:80]
        action = (data.get('action') or '').strip()[:40]
        coins = data.get('coins')
        if not anon_id or not action:
            return jsonify({'status': 'ignored'})

        conn = get_db()
        # The User-Agent header comes from the browser itself with every
        # request — no extra permission or frontend change needed to
        # read it. Parsed into a short summary and stored alongside the
        # event so the admin dashboard can show which device/browser
        # each user is on.
        device_info = parse_device_info(request.headers.get('User-Agent', ''))
        # Read the signed-in email from the SERVER-SIDE session (not
        # trusting anything the client might send) — NULL for guests.
        user_email = session.get('user_email')
        conn.execute(
            "INSERT INTO usage_events (anon_id, user_name, action, created_at, device_info, user_email) VALUES (?, ?, ?, ?, ?, ?)",
            (anon_id, user_name, action, datetime.now(timezone.utc).isoformat(), device_info, user_email)
        )
        if isinstance(coins, int):
            conn.execute(
                """INSERT INTO user_state (anon_id, coins, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(anon_id) DO UPDATE SET coins = excluded.coins, updated_at = excluded.updated_at""",
                (anon_id, coins, datetime.now(timezone.utc).isoformat())
            )
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        print(f"⚠️ Track event failed (non-critical): {e}")
        return jsonify({'status': 'error'}), 200  # 200 on purpose — never surface this as a real error to the client


# FIX (urgent performance issue): _is_banned() used to open a fresh
# Turso connection and run a query on EVERY SINGLE /process-note and
# /tts request — adding a real network round-trip to Turso before any
# actual note/audio work even began, on every request, for every
# user, even though bans are rare and change infrequently. This
# directly contributed to the app feeling much slower after the ban
# feature was added. Now caches the banned-identity list in memory,
# refreshed at most once every 30 seconds, so the vast majority of
# requests check a local Python set (near-instant) instead of hitting
# the database at all.
_banned_identities_cache = set()
_banned_cache_last_refresh = 0
_banned_cache_lock = threading.Lock()
BANNED_CACHE_TTL_SECONDS = 30


def _refresh_banned_cache_if_stale():
    global _banned_identities_cache, _banned_cache_last_refresh
    now = time.time()
    if now - _banned_cache_last_refresh < BANNED_CACHE_TTL_SECONDS:
        return
    with _banned_cache_lock:
        # Re-check after acquiring the lock — another thread may have
        # just refreshed it while we were waiting.
        if time.time() - _banned_cache_last_refresh < BANNED_CACHE_TTL_SECONDS:
            return
        try:
            conn = get_db()
            rows = conn.execute("SELECT identity FROM banned_identities").fetchall()
            conn.close()
            _banned_identities_cache = {row['identity'] for row in rows}
            _banned_cache_last_refresh = time.time()
        except Exception as e:
            print(f"⚠️ Banned-identity cache refresh failed (keeping previous cache): {e}")
            # Don't update the timestamp — we'll try again on the next
            # request rather than waiting the full TTL after a failure.


def _is_banned(anon_id=None, email=None):
    """Checks whether a device (anon_id) or account (email) has been
    banned by an admin. Either match blocks the action. Uses an
    in-memory cache (see above) instead of a database round-trip on
    every call."""
    if not anon_id and not email:
        return False
    try:
        _refresh_banned_cache_if_stale()
        if anon_id and anon_id in _banned_identities_cache:
            return True
        if email and email in _banned_identities_cache:
            return True
        return False
    except Exception as e:
        print(f"⚠️ Ban check failed (allowing request through): {e}")
        return False  # fail open — never block legitimate users due to a hiccup


def _is_admin_logged_in():
    return session.get('is_admin') is True


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        # FIX (security gap): admin login had NO protection against
        # brute-force password guessing — a bot could try unlimited
        # passwords. Now capped at 5 login ATTEMPTS per 5 minutes per
        # IP (just viewing the page doesn't count), with the exact
        # wait time shown right on the login page itself.
        identifier = request.remote_addr or 'unknown'
        allowed, retry_after = _check_rate_limit(f"admin_login:{identifier}", 5, 300)
        if not allowed:
            mins, secs = divmod(retry_after, 60)
            wait_str = f'මිනිත්තු {mins}ක් {secs} තත්පරයක්' if mins > 0 else f'තත්පර {secs}ක්'
            error = f'ඉතා වේගවත් ලෙස උත්සාහ කරලා තියෙනවා — {wait_str} ඉන්න, ඊට පස්සේ නැවත උත්සාහ කරන්න.'
            return render_template('admin_login.html', error=error)

        password = request.form.get('password', '')
        if password == ADMIN_PASSWORD:
            session.permanent = True
            session['is_admin'] = True
            return redirect(url_for('admin_dashboard'))
        error = 'වැරදි password එකක්.'
    return render_template('admin_login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))


def _get_admin_usage_data():
    conn = get_db()
    users = conn.execute("""
        SELECT
            anon_id,
            (SELECT user_name FROM usage_events ue2
             WHERE ue2.anon_id = ue1.anon_id AND ue2.user_name != ''
             ORDER BY ue2.created_at DESC LIMIT 1) AS latest_name,
            (SELECT device_info FROM usage_events ue3
             WHERE ue3.anon_id = ue1.anon_id AND ue3.device_info IS NOT NULL
             ORDER BY ue3.created_at DESC LIMIT 1) AS device_info,
            (SELECT GROUP_CONCAT(email_col, '||') FROM (
                SELECT user_email AS email_col, MAX(created_at) AS last_ts
                FROM usage_events ue4
                WHERE ue4.anon_id = ue1.anon_id AND ue4.user_email IS NOT NULL
                GROUP BY user_email
                ORDER BY last_ts DESC
            )) AS email_history,
            MIN(created_at) AS first_seen,
            MAX(created_at) AS last_seen,
            COUNT(*) AS total_events,
            SUM(CASE WHEN action = 'note_processed' THEN 1 ELSE 0 END) AS notes_processed,
            SUM(CASE WHEN action = 'audio_generated' THEN 1 ELSE 0 END) AS audio_generated,
            -- FIX: for a signed-in account, users.coins (keyed by
            -- email) is the REAL, authoritative, cross-device balance
            -- — the same one the Admin's "Add Coins" feature updates.
            -- The old query only ever read user_state.coins (a
            -- device-level snapshot), so admin-added coins never
            -- showed up here even though the real balance WAS correct.
            -- Falls back to user_state.coins only for guests (who have
            -- no users row at all).
            COALESCE(
                (SELECT u.coins FROM users u WHERE u.email = (
                    SELECT user_email FROM usage_events ue4
                    WHERE ue4.anon_id = ue1.anon_id AND ue4.user_email IS NOT NULL
                    ORDER BY ue4.created_at DESC LIMIT 1
                )),
                (SELECT coins FROM user_state us WHERE us.anon_id = ue1.anon_id)
            ) AS coins
        FROM usage_events ue1
        GROUP BY anon_id
        ORDER BY last_seen DESC
    """).fetchall()

    total_notes_in_library = conn.execute('SELECT COUNT(*) FROM notes').fetchone()[0]

    # Gemini calls so far THIS calendar month — lets the admin watch
    # usage approach the free-tier monthly quota before it's actually
    # exhausted, instead of finding out when requests start failing.
    month_start = datetime.now(timezone.utc).strftime('%Y-%m-01')
    gemini_calls_row = conn.execute(
        'SELECT COUNT(*) FROM gemini_calls WHERE created_at >= ?', (month_start,)
    ).fetchone()
    gemini_calls_this_month = gemini_calls_row[0] if gemini_calls_row else 0

    conn.close()

    sl_offset = timedelta(hours=5, minutes=30)
    now_utc = datetime.now(timezone.utc)

    def to_sl_time(iso_str):
        if not iso_str:
            return ''
        try:
            dt = datetime.fromisoformat(iso_str)
            return (dt + sl_offset).strftime('%Y-%m-%d %H:%M')
        except Exception:
            return iso_str[:16].replace('T', ' ')

    def is_online(iso_str):
        if not iso_str:
            return False
        try:
            dt = datetime.fromisoformat(iso_str)
            return (now_utc - dt) <= timedelta(minutes=5)
        except Exception:
            return False

    users_list = []
    for row in users:
        u = dict(row)
        u['is_online'] = is_online(u['last_seen'])
        u['first_seen'] = to_sl_time(u['first_seen'])
        u['last_seen'] = to_sl_time(u['last_seen'])
        # Splits the '||'-joined, most-recent-first email history into
        # the CURRENT email (whichever account is signed in on this
        # device right now — shown in green) and any OLDER emails this
        # same device has logged into before (shown in red), so the
        # admin can see when a device switched accounts.
        raw_history = u.pop('email_history', None)
        emails = [e for e in (raw_history.split('||') if raw_history else []) if e]
        u['current_email'] = emails[0] if emails else None
        u['old_emails'] = emails[1:] if len(emails) > 1 else []
        users_list.append(u)

    return {
        'users': users_list,
        'total_users': len(users_list),
        'total_notes_in_library': total_notes_in_library,
        'gemini_calls_this_month': gemini_calls_this_month,
        # Tells the admin whether data is genuinely persisting to
        # Turso, or has silently fallen back to the ephemeral local
        # SQLite file (e.g. if TURSO_DATABASE_URL/TURSO_AUTH_TOKEN are
        # missing or invalid) — this is the real thing worth verifying,
        # since once this is 'turso', EVERY user's data goes through
        # the same persistent backend.
        'storage_backend': 'turso' if USE_TURSO else 'sqlite',
    }


def _get_daily_activity(days=7):
    conn = get_db()
    rows = conn.execute("""
        SELECT
            substr(created_at, 1, 10) AS day,
            SUM(CASE WHEN action = 'note_processed' THEN 1 ELSE 0 END) AS notes,
            SUM(CASE WHEN action = 'audio_generated' THEN 1 ELSE 0 END) AS audio
        FROM usage_events
        GROUP BY day
        ORDER BY day DESC
        LIMIT ?
    """, (days,)).fetchall()
    conn.close()
    result = [dict(row) for row in rows]
    result.reverse()
    return result


def _get_top_subjects(limit=8):
    conn = get_db()
    rows = conn.execute("""
        SELECT subject, COUNT(*) AS note_count
        FROM notes
        GROUP BY subject
        ORDER BY note_count DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _get_feature_usage():
    conn = get_db()
    rows = conn.execute("""
        SELECT action, COUNT(*) AS count
        FROM usage_events
        GROUP BY action
        ORDER BY count DESC
    """).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _get_user_growth(days=14):
    conn = get_db()
    rows = conn.execute("""
        SELECT substr(first_seen, 1, 10) AS day, COUNT(*) AS new_users
        FROM (
            SELECT anon_id, MIN(created_at) AS first_seen
            FROM usage_events
            GROUP BY anon_id
        )
        GROUP BY day
        ORDER BY day ASC
    """).fetchall()
    conn.close()

    all_days = [dict(row) for row in rows]
    running_total = 0
    result = []
    cutoff_days = all_days[-days:] if len(all_days) > days else all_days
    earlier_total = sum(d['new_users'] for d in all_days[:-days]) if len(all_days) > days else 0
    running_total = earlier_total
    for d in cutoff_days:
        running_total += d['new_users']
        result.append({'day': d['day'], 'new_users': d['new_users'], 'cumulative': running_total})
    return result


@app.route('/admin/feature-usage')
def admin_feature_usage():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        return jsonify({'status': 'success', 'features': _get_feature_usage()})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/user-growth')
def admin_user_growth():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        return jsonify({'status': 'success', 'growth': _get_user_growth()})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/full-backup')
def admin_full_backup():
    if not _is_admin_logged_in():
        return redirect(url_for('admin_login'))
    try:
        conn = get_db()
        notes = [dict(row) for row in conn.execute('SELECT * FROM notes').fetchall()]
        usage_events = [dict(row) for row in conn.execute('SELECT * FROM usage_events').fetchall()]
        announcements = [dict(row) for row in conn.execute('SELECT * FROM announcements').fetchall()]
        conn.close()

        backup = {
            'exported_at': datetime.now(timezone.utc).isoformat(),
            'notes': notes,
            'usage_events': usage_events,
            'announcements': announcements,
        }
        response = app.response_class(
            json.dumps(backup, indent=2, ensure_ascii=False),
            mimetype='application/json'
        )
        date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        response.headers['Content-Disposition'] = f'attachment; filename=notewav_full_backup_{date_str}.json'
        return response
    except Exception as e:
        return f"Backup error: {e}", 500


@app.route('/admin/data')
def admin_data():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        return jsonify({'status': 'success', **_get_admin_usage_data()})
    except Exception as e:
        print(f"❌ Admin data error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/activity-chart')
def admin_activity_chart():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        return jsonify({'status': 'success', 'days': _get_daily_activity()})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/top-subjects')
def admin_top_subjects():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        return jsonify({'status': 'success', 'subjects': _get_top_subjects()})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/add-coins', methods=['POST'])
def admin_add_coins():
    """Adds coins to a signed-in user's REAL, server-side balance
    (the users.coins column) — not just a local display trick. This
    only works for accounts that have signed in with Google at least
    once (identified by email); a device that's only ever used the app
    as a guest has no server-side coin record to add to.

    The added coins reach the person automatically the next time their
    browser loads the app — checkGoogleAuthStatus() already pulls the
    server's coins value down and overwrites localStorage with it on
    every page load for signed-in users, so no extra sync code is
    needed here."""
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        data = request.get_json(silent=True) or {}
        email = (data.get('email') or '').strip()
        amount = data.get('amount')

        if not email:
            return jsonify({'status': 'error', 'message': 'මේ user කෙනෙක් login වෙලා නෑ — coins add කරන්න බැහැ.'}), 400
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'message': 'වලංගු coins ප්‍රමාණයක් නෑ.'}), 400
        if amount == 0:
            return jsonify({'status': 'error', 'message': 'Coins ප්‍රමාණය 0 විය නොහැක.'}), 400
        if abs(amount) > 100000:
            return jsonify({'status': 'error', 'message': 'Coins ප්‍රමාණය ඉතා විශාලයි.'}), 400

        conn = get_db()
        existing = conn.execute("SELECT google_id, coins FROM users WHERE email = ?", (email,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({'status': 'error', 'message': 'මේ email එකෙන් account එකක් හම්බුනේ නෑ.'}), 404

        new_balance = (existing['coins'] or 0) + amount
        conn.execute("UPDATE users SET coins = ? WHERE email = ?", (new_balance, email))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'new_balance': new_balance})
    except Exception as e:
        print(f"❌ Admin add-coins error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/user-timeline/<anon_id>')
def admin_user_timeline(anon_id):
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT action, user_name, created_at FROM usage_events WHERE anon_id = ? ORDER BY created_at DESC",
            (anon_id,)
        ).fetchall()

        # NEW: also report whether this device (or its associated
        # account email, if signed in) is CURRENTLY banned — the admin
        # dashboard needs this to show "Ban" vs "Unban" correctly when
        # the timeline modal is opened, instead of always assuming
        # "not banned".
        email_row = conn.execute(
            "SELECT user_email FROM usage_events WHERE anon_id = ? AND user_email IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (anon_id,)
        ).fetchone()
        latest_email = email_row['user_email'] if email_row else None

        is_banned_row = conn.execute(
            "SELECT identity FROM banned_identities WHERE identity = ? OR identity = ?",
            (anon_id, latest_email or anon_id)
        ).fetchone()
        is_banned = is_banned_row is not None

        conn.close()

        sl_offset = timedelta(hours=5, minutes=30)
        events = []
        for row in rows:
            e = dict(row)
            try:
                dt = datetime.fromisoformat(e['created_at'])
                e['created_at'] = (dt + sl_offset).strftime('%Y-%m-%d %H:%M')
            except Exception:
                pass
            events.append(e)
        return jsonify({'status': 'success', 'events': events, 'is_banned': is_banned})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/notes-list')
def admin_notes_list():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, subject, title, note_text, created_at, user_name FROM notes ORDER BY created_at DESC"
        ).fetchall()
        conn.close()

        sl_offset = timedelta(hours=5, minutes=30)
        notes = []
        for row in rows:
            n = dict(row)
            snippet = (n.get('note_text') or '')[:120]
            n['snippet'] = snippet + ('...' if len(n.get('note_text') or '') > 120 else '')
            n.pop('note_text', None)
            try:
                dt = datetime.fromisoformat(n['created_at'])
                n['created_at'] = (dt + sl_offset).strftime('%Y-%m-%d %H:%M')
            except Exception:
                pass
            notes.append(n)
        return jsonify({'status': 'success', 'notes': notes})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/notes/<int:note_id>/delete', methods=['POST'])
def admin_delete_note(note_id):
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        conn = get_db()
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/notes/bulk-delete', methods=['POST'])
def admin_bulk_delete_notes():
    """NEW: delete several library notes at once — used by the admin
    dashboard's checkbox-select UI, instead of deleting one at a time."""
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        data = request.get_json(silent=True) or {}
        note_ids = data.get('note_ids') or []
        note_ids = [int(i) for i in note_ids if str(i).isdigit()][:200]  # sanity cap
        if not note_ids:
            return jsonify({'status': 'error', 'message': 'No note IDs provided.'}), 400
        conn = get_db()
        placeholders = ','.join('?' * len(note_ids))
        conn.execute(f"DELETE FROM notes WHERE id IN ({placeholders})", note_ids)
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'deleted': len(note_ids)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/ban', methods=['POST'])
def admin_ban_identity():
    """NEW: bans (or unbans) a device (anon_id) or account (email) —
    toggles based on current state. A banned identity can no longer
    process notes or generate audio; their existing saved notes are
    NOT deleted."""
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        data = request.get_json(silent=True) or {}
        identity = (data.get('identity') or '').strip()
        identity_type = (data.get('identity_type') or 'anon_id').strip()
        reason = (data.get('reason') or '').strip()[:200]
        if not identity:
            return jsonify({'status': 'error', 'message': 'No identity provided.'}), 400

        conn = get_db()
        existing = conn.execute("SELECT 1 FROM banned_identities WHERE identity = ?", (identity,)).fetchone()
        if existing:
            conn.execute("DELETE FROM banned_identities WHERE identity = ?", (identity,))
            action = 'unbanned'
        else:
            conn.execute(
                "INSERT INTO banned_identities (identity, identity_type, reason, banned_at) VALUES (?, ?, ?, ?)",
                (identity, identity_type, reason, datetime.now(timezone.utc).isoformat())
            )
            action = 'banned'
        conn.commit()
        conn.close()
        # Force the in-memory cache to refresh on the next check —
        # otherwise a fresh ban/unban wouldn't take effect for up to
        # BANNED_CACHE_TTL_SECONDS.
        global _banned_cache_last_refresh
        _banned_cache_last_refresh = 0
        return jsonify({'status': 'success', 'action': action})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/library/report/<int:note_id>', methods=['POST'])
@rate_limited(5, 300)
def report_note(note_id):
    """NEW (public): lets any student flag a Library note as
    inappropriate/spam — reviewed by the admin in a dedicated Reports
    section, rather than the note just silently staying up."""
    try:
        data = request.get_json(silent=True) or {}
        reason = (data.get('reason') or '').strip()[:200]
        conn = get_db()
        note_exists = conn.execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone()
        if not note_exists:
            conn.close()
            return jsonify({'status': 'error', 'message': 'Note not found.'}), 404
        conn.execute(
            "INSERT INTO note_reports (note_id, reason, created_at) VALUES (?, ?, ?)",
            (note_id, reason, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': 'Report එක ලැබුණා. ස්තූතියි!'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/reports')
def admin_reports():
    """NEW: lists open (not-yet-dismissed) note reports for the admin,
    joined with the note's own title/subject so they don't need to
    look it up separately."""
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT r.id, r.note_id, r.reason, r.created_at,
                   n.title, n.subject, n.note_text
            FROM note_reports r
            LEFT JOIN notes n ON n.id = r.note_id
            WHERE r.dismissed = 0
            ORDER BY r.created_at DESC
        """).fetchall()
        conn.close()
        return jsonify({'status': 'success', 'reports': [dict(row) for row in rows]})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/reports/<int:report_id>/dismiss', methods=['POST'])
def admin_dismiss_report(report_id):
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        conn = get_db()
        conn.execute("UPDATE note_reports SET dismissed = 1 WHERE id = ?", (report_id,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/insights')
def admin_insights():
    """NEW: one consolidated endpoint for several admin dashboard
    additions — coins economy total, level distribution, weekly/
    monthly active user counts, a Top-10 leaderboard by notes
    processed, and a live snapshot of recent Gemini TTS call volume
    (a proxy for how close the app is to Google's own rate limit)."""
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        conn = get_db()

        # --- Coins economy ---
        signed_in_coins = conn.execute("SELECT COALESCE(SUM(coins), 0) AS total FROM users").fetchone()['total']
        guest_coins = conn.execute("SELECT COALESCE(SUM(coins), 0) AS total FROM user_state").fetchone()['total']

        # NEW: estimated storage used by saved photo attachments — since
        # images are stored as base64 text directly in the (persistent,
        # free-tier-capped) database rather than as files, this is
        # worth watching so a real capacity issue is caught early
        # rather than as a surprise when the database suddenly rejects
        # writes.
        image_stats = conn.execute("""
            SELECT COUNT(*) AS image_count, COALESCE(SUM(LENGTH(source_image_data)), 0) AS total_bytes
            FROM notes WHERE source_image_data IS NOT NULL
        """).fetchone()
        image_storage_mb = round(image_stats['total_bytes'] / (1024 * 1024), 2)
        FREE_TIER_STORAGE_GB = 5  # Turso free tier, as of 2026
        image_storage_pct_of_free_tier = round((image_stats['total_bytes'] / (FREE_TIER_STORAGE_GB * 1024 * 1024 * 1024)) * 100, 2)

        # --- Notes processed per device (drives level distribution + leaderboard) ---
        per_device = conn.execute("""
            SELECT anon_id,
                   (SELECT user_name FROM usage_events ue2 WHERE ue2.anon_id = ue1.anon_id AND ue2.user_name IS NOT NULL ORDER BY ue2.created_at DESC LIMIT 1) AS display_name,
                   SUM(CASE WHEN action = 'note_processed' THEN 1 ELSE 0 END) AS notes_processed
            FROM usage_events ue1
            GROUP BY anon_id
            HAVING notes_processed > 0
        """).fetchall()

        # Mirrors the frontend's LEVEL_THRESHOLDS exactly.
        level_thresholds = [
            (1, 0, '🌱 Beginner'), (2, 5, '📖 Learner'), (3, 10, '✏️ Note Taker'),
            (4, 20, '🎓 Scholar'), (5, 40, '🧠 Expert'), (6, 75, '🏆 Master'), (7, 150, '👑 Legend'),
        ]

        def level_for_count(n):
            current = level_thresholds[0]
            for lvl in level_thresholds:
                if n >= lvl[1]:
                    current = lvl
            return current[2]

        level_distribution = {}
        for row in per_device:
            label = level_for_count(row['notes_processed'])
            level_distribution[label] = level_distribution.get(label, 0) + 1

        leaderboard = sorted(
            [{'name': r['display_name'] or 'Anonymous', 'notes_processed': r['notes_processed']} for r in per_device],
            key=lambda x: x['notes_processed'], reverse=True
        )[:10]

        # --- Retention: distinct devices active in the last 7 / 30 days ---
        now = datetime.now(timezone.utc)
        wau_cutoff = (now - timedelta(days=7)).isoformat()
        mau_cutoff = (now - timedelta(days=30)).isoformat()
        wau = conn.execute("SELECT COUNT(DISTINCT anon_id) AS c FROM usage_events WHERE created_at >= ?", (wau_cutoff,)).fetchone()['c']
        mau = conn.execute("SELECT COUNT(DISTINCT anon_id) AS c FROM usage_events WHERE created_at >= ?", (mau_cutoff,)).fetchone()['c']

        conn.close()

        # --- Gemini TTS live load (proxy for quota headroom) ---
        with _gemini_tts_gate_lock:
            gemini_recent_calls = len(_gemini_tts_call_times)

        return jsonify({
            'status': 'success',
            'coins_economy': {'signed_in_total': signed_in_coins, 'guest_total': guest_coins},
            'level_distribution': level_distribution,
            'leaderboard': leaderboard,
            'retention': {'weekly_active': wau, 'monthly_active': mau},
            'gemini_load': {'recent_calls_last_60s': gemini_recent_calls, 'safe_limit_per_worker': GEMINI_TTS_SAFE_RPM_PER_WORKER},
            'image_storage': {
                'image_count': image_stats['image_count'],
                'total_mb': image_storage_mb,
                'pct_of_free_tier': image_storage_pct_of_free_tier,
                'free_tier_gb': FREE_TIER_STORAGE_GB,
            },
        })
    except Exception as e:
        print(f"❌ Admin insights error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/announcements-list')
def admin_announcements_list():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, message, created_at, target_anon_id, scheduled_at FROM announcements ORDER BY id DESC LIMIT 30"
        ).fetchall()

        sl_offset = timedelta(hours=5, minutes=30)
        items = []
        for row in rows:
            a = dict(row)
            try:
                dt = datetime.fromisoformat(a['created_at'])
                a['created_at'] = (dt + sl_offset).strftime('%Y-%m-%d %H:%M')
            except Exception:
                pass
            # For private (per-device) messages, look up that device's
            # latest known display name — lets the admin CONFIRM exactly
            # who a private message was targeted at (useful for
            # verifying it went to the right/currently-active device,
            # e.g. after a cache clear regenerated someone's anon_id).
            if a.get('target_anon_id'):
                name_row = conn.execute(
                    "SELECT user_name FROM usage_events WHERE anon_id = ? AND user_name != '' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (a['target_anon_id'],)
                ).fetchone()
                a['target_name'] = name_row['user_name'] if name_row and name_row['user_name'] else None
            else:
                a['target_name'] = None
            items.append(a)
        conn.close()
        return jsonify({'status': 'success', 'announcements': items})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/announcements/<int:ann_id>/delete', methods=['POST'])
def admin_delete_announcement(ann_id):
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        conn = get_db()
        conn.execute("DELETE FROM announcements WHERE id = ?", (ann_id,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/export-csv')
def admin_export_csv():
    if not _is_admin_logged_in():
        return redirect(url_for('admin_login'))
    try:
        data = _get_admin_usage_data()
        lines = ['Name,First Seen (LK),Last Seen (LK),Notes Processed,Audio Generated,Total Actions,Online Now']
        for u in data['users']:
            name = (u['latest_name'] or 'Unnamed').replace(',', ' ')
            lines.append(
                f"{name},{u['first_seen']},{u['last_seen']},{u['notes_processed']},"
                f"{u['audio_generated']},{u['total_events']},{'Yes' if u['is_online'] else 'No'}"
            )
        csv_content = '\n'.join(lines)
        response = app.response_class(csv_content, mimetype='text/csv')
        response.headers['Content-Disposition'] = 'attachment; filename=notewav_usage.csv'
        return response
    except Exception as e:
        return f"CSV export error: {e}", 500


@app.route('/admin/announce', methods=['POST'])
def admin_announce():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        data = request.get_json(silent=True) or {}
        message = (data.get('message') or '').strip()[:500]
        # NEW: optional — if provided, this notification is PRIVATE to
        # just that one device (anon_id). Left out/empty, it broadcasts
        # to everyone exactly as announcements always have.
        target_anon_id = (data.get('target_anon_id') or '').strip()[:64] or None
        # NEW: optional ISO datetime string — if in the future, this
        # announcement stays hidden from students until that time
        # arrives (checked on every /announcements/list poll, no
        # separate scheduler/cron job needed).
        scheduled_at = (data.get('scheduled_at') or '').strip() or None
        if not message:
            return jsonify({'status': 'error', 'message': 'Message එකක් ලියන්න.'}), 400
        conn = get_db()
        conn.execute(
            "INSERT INTO announcements (message, created_at, target_anon_id, scheduled_at) VALUES (?, ?, ?, ?)",
            (message, datetime.now(timezone.utc).isoformat(), target_anon_id, scheduled_at)
        )
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        print(f"❌ Admin announce error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/announcements/list')
def announcements_list():
    """Public — returns the recent LIST of announcements relevant to
    this device (global broadcasts + any private ones targeted at this
    anon_id), newest first, capped at 20. FIX: the older
    /announcements/latest only ever returned the single most recent
    message — if admin sent several announcements close together, a
    student who hadn't checked in a while would only ever see the
    LAST one and never know earlier ones existed. This lets the
    notification popup show the full recent list instead."""
    try:
        anon_id = (request.args.get('anon_id') or '').strip()[:64]
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        if anon_id:
            rows = conn.execute(
                "SELECT id, message, created_at FROM announcements "
                "WHERE (target_anon_id IS NULL OR target_anon_id = ?) "
                "AND (scheduled_at IS NULL OR scheduled_at <= ?) "
                "ORDER BY id DESC LIMIT 20",
                (anon_id, now_iso)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, message, created_at FROM announcements "
                "WHERE target_anon_id IS NULL AND (scheduled_at IS NULL OR scheduled_at <= ?) "
                "ORDER BY id DESC LIMIT 20",
                (now_iso,)
            ).fetchall()
        conn.close()
        return jsonify({'status': 'success', 'announcements': [dict(row) for row in rows]})
    except Exception as e:
        print(f"❌ Announcements list fetch error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/announcements/latest')
def announcements_latest():
    """Public — every visitor's browser polls this periodically to
    check for a new admin message. NEW: also considers PRIVATE
    (per-device) notifications — a device's own anon_id is passed as a
    query param, and this returns whichever announcement is most
    recent between the global broadcasts and that device's own private
    messages. No anon_id provided (e.g. an older cached frontend) falls
    back to broadcast-only, the original behavior."""
    try:
        anon_id = (request.args.get('anon_id') or '').strip()[:64]
        conn = get_db()
        if anon_id:
            row = conn.execute(
                "SELECT id, message, created_at FROM announcements "
                "WHERE target_anon_id IS NULL OR target_anon_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (anon_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, message, created_at FROM announcements WHERE target_anon_id IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        conn.close()
        if not row:
            return jsonify({'status': 'success', 'announcement': None})
        return jsonify({'status': 'success', 'announcement': dict(row)})
    except Exception as e:
        print(f"❌ Announcements fetch error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin')
def admin_dashboard():
    if not _is_admin_logged_in():
        return redirect(url_for('admin_login'))

    try:
        data = _get_admin_usage_data()
        return render_template('admin_dashboard.html', **data)
    except Exception as e:
        print(f"❌ Admin dashboard error: {e}")
        return f"Dashboard load කරගැනීම අසාර්ථක විය: {e}", 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    debug_mode = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(debug=debug_mode, host='0.0.0.0', port=port)