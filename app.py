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
    # FIX (Aug 19, 2026 — diagnostic ⏱️ logs weren't appearing in
    # Render's live log stream at all, or all appeared bunched together
    # with near-identical timestamps only when a worker restarted):
    # re-wrapping stdout/stderr like this, WITHOUT specifying
    # line_buffering=True, leaves Python free to use its default block
    # buffering for a non-interactive stream (which a container's
    # stdout always is) — meaning every print() call was being held in
    # memory and only actually sent to Render's logs once that buffer
    # filled up or the process exited/restarted. That's exactly why
    # print() statements added specifically to time slow requests
    # (gTTS, Gemini) were invisible in real time. line_buffering=True
    # forces a flush after every newline, so print()'d logs now show up
    # immediately, as they happen.
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    if hasattr(sys.stderr, 'buffer'):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)
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
    ONE persistent client across the entire app's lifetime."""

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
    for column_def in ["anon_id TEXT", "user_name TEXT"]:
        try:
            conn.execute(f"ALTER TABLE notes ADD COLUMN {column_def}")
        except Exception:
            pass  # column already exists
    try:
        conn.execute("ALTER TABLE notes ADD COLUMN owner_google_id TEXT")
    except Exception:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE notes ADD COLUMN source_image_data TEXT")
    except Exception:
        pass  # column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anon_id TEXT NOT NULL,
            user_name TEXT,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    try:
        conn.execute("ALTER TABLE usage_events ADD COLUMN device_info TEXT")
    except Exception:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE usage_events ADD COLUMN user_email TEXT")
    except Exception:
        pass  # column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            anon_id TEXT PRIMARY KEY,
            coins INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    try:
        conn.execute("ALTER TABLE announcements ADD COLUMN target_anon_id TEXT")
    except Exception:
        pass  # column already exists
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gemini_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL
        )
    """)
    # NEW (Aug 20, 2026): powers 4 admin dashboard insights — TTS/script
    # engine breakdown, error-rate tracking per engine, and processing
    # time trends. Kept as its own lightweight table (not bolted onto
    # usage_events) so it can log BOTH successes and failures with rich
    # detail (duration, exact error) without cluttering the simpler
    # usage_events table that only records that an action happened.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS engine_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            engine TEXT NOT NULL,
            model_version TEXT,
            duration_seconds REAL,
            success INTEGER NOT NULL,
            error_message TEXT,
            device_info TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS banned_identities (
            identity TEXT PRIMARY KEY,
            identity_type TEXT NOT NULL,
            reason TEXT,
            banned_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            dismissed INTEGER DEFAULT 0
        )
    """)
    try:
        conn.execute("ALTER TABLE announcements ADD COLUMN scheduled_at TEXT")
    except Exception:
        pass  # column already exists

    # ========================================
    # DIGITAL TUITION SIR — courses (subject/grade offerings, split from
    # an admin-uploaded syllabus PDF into day-by-day lessons). Kept as
    # its own set of tables (not bolted onto `notes`) since a course
    # lesson is authored ONCE by an admin and served identically to
    # every enrolled student, unlike a `notes` row which is one
    # student's own private note.
    # ========================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grade TEXT NOT NULL,
            subject TEXT NOT NULL,
            title TEXT NOT NULL,
            exam_target TEXT,
            price_lkr INTEGER,
            free_trial_days INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS course_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            day_number INTEGER NOT NULL,
            title TEXT NOT NULL,
            source_text TEXT NOT NULL,
            script_text TEXT,
            mermaid_code_si TEXT,
            mermaid_code_en TEXT,
            ai_audio_url TEXT,
            ai_audio_engine TEXT,
            manual_audio_url TEXT,
            active_audio_source TEXT NOT NULL DEFAULT 'ai',
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            UNIQUE(course_id, day_number)
        )
    """)
    # Sentence-level timings for the student player's word-by-word
    # highlight (same feature the quick-note flow already has) — only
    # meaningful for ai_audio_url (TTS knows its own timing); a manual
    # upload has no timing data, so this stays NULL for those and the
    # player falls back to plain (unhighlighted) text.
    try:
        conn.execute("ALTER TABLE course_lessons ADD COLUMN ai_audio_timings TEXT")
    except Exception:
        pass  # column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_course_enrollments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_google_id TEXT NOT NULL,
            course_id INTEGER NOT NULL,
            enrolled_at TEXT NOT NULL,
            UNIQUE(user_google_id, course_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_lesson_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_google_id TEXT NOT NULL,
            lesson_id INTEGER NOT NULL,
            completed_at TEXT NOT NULL,
            UNIQUE(user_google_id, lesson_id)
        )
    """)
    # A large syllabus PDF (up to ~200 pages) is OCR'd + split across
    # many small requests (see admin_pdf_job_start/ocr-step/split-step)
    # rather than in one HTTP request or a background thread — each
    # step needs to read/update progress here, and different steps can
    # land on different gunicorn workers, so this can't just be an
    # in-memory dict.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pdf_split_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'processing',
            total_pages INTEGER,
            processed_pages INTEGER DEFAULT 0,
            result_json TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # FIX (Aug 27, 2026): a background thread doing the OCR+split work
    # (kicked off by the request, running after it returned) hung
    # silently in production TWICE — once with concurrent Gemini calls,
    # once after reverting to sequential — while every plain synchronous
    # Gemini call made from a normal request-handling thread has been
    # reliable all session. That points at something specific to
    # background threads under this gunicorn deployment (most likely a
    # fork()-inherited lock on the shared client), not the calls
    # themselves. New approach: no background thread at all — the
    # browser drives one page-batch or one lesson-chunk per request,
    # each a normal fast synchronous call. These columns track that.
    for column_def in [
        "phase TEXT DEFAULT 'ocr'",
        "full_text TEXT",
        "total_chunks INTEGER DEFAULT 0",
        "processed_chunks INTEGER DEFAULT 0",
        "min_words INTEGER DEFAULT 100",
        "max_words INTEGER DEFAULT 220",
    ]:
        try:
            conn.execute(f"ALTER TABLE pdf_split_jobs ADD COLUMN {column_def}")
        except Exception:
            pass  # column already exists

    conn.commit()
    conn.close()


init_db()


def log_engine_event(event_type, engine, model_version=None, duration_seconds=None, success=True, error_message=None):
    """Best-effort logging for the admin dashboard's Engine Health
    insights (breakdown, error rates, processing time trends). Never
    raises — a logging failure must never break the actual feature."""
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO engine_events
               (event_type, engine, model_version, duration_seconds, success, error_message, device_info, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_type, engine, model_version,
                round(duration_seconds, 2) if duration_seconds is not None else None,
                1 if success else 0,
                (error_message or '')[:300] if error_message else None,
                parse_device_info(request.headers.get('User-Agent', '')) if request else None,
                datetime.now(timezone.utc).isoformat(),
            )
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Engine event logging failed (non-critical): {e}")

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
# API RATE LIMITING
# ========================================
_rate_limit_lock = threading.Lock()
_rate_limit_buckets = defaultdict(deque)


def _check_rate_limit(key, max_requests, window_seconds):
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
# CLOUDINARY (persistent storage for Digital Tuition Sir lesson audio)
# ========================================
# Render's local disk is EPHEMERAL — wiped on every redeploy/restart
# (this is exactly why the DB uses Turso instead of local SQLite; see
# USE_TURSO above). A course lesson's audio is meant to be permanent
# (served to every enrolled student, indefinitely), so it can't live
# on local disk in production. Cloudinary reads its config from the
# CLOUDINARY_URL env var automatically (cloudinary://key:secret@cloud).
CLOUDINARY_AVAILABLE = False
if os.environ.get('CLOUDINARY_URL'):
    try:
        import cloudinary
        import cloudinary.uploader
        CLOUDINARY_AVAILABLE = True
        print("✅ Cloudinary (persistent lesson audio storage) is ready!")
    except Exception as e:
        print(f"⚠️ Cloudinary setup failed: {e}")
else:
    print("⚠️ CLOUDINARY_URL not set — lesson audio will fall back to local disk (LOST on every redeploy — set this before real students use Digital Tuition Sir).")

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

# ========================================
# OPENAI API (Text-to-Speech — GA/stable alternative to Gemini TTS)
# ========================================
# NEW (Aug 19, 2026): added as a more reliable Natural Voice option.
# Unlike Gemini's TTS models (all still "preview" status with no SLA —
# see the GEMINI_THINKING_BUDGET notes above for the full history of
# instability this caused), OpenAI's TTS models (gpt-4o-mini-tts,
# tts-1, tts-1-hd) are GA/production models with normal rate limits
# and no preview-status caveats. Sinhala isn't on OpenAI's officially
# listed language table, but manual testing in the OpenAI Playground
# confirmed usable Sinhala output — good enough to offer as a second,
# independent Natural Voice engine alongside Gemini, not a replacement.
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        print("✅ OpenAI API is ready!")
    except Exception as e:
        openai_client = None
        print(f"⚠️ OpenAI client setup failed: {e}")
else:
    openai_client = None
    print("⚠️ OPENAI_API_KEY not found — OpenAI Voice option will be unavailable")

SAFETY_SETTINGS = [
    types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='BLOCK_MEDIUM_AND_ABOVE'),
    types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='BLOCK_MEDIUM_AND_ABOVE'),
    types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='BLOCK_MEDIUM_AND_ABOVE'),
    types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='BLOCK_MEDIUM_AND_ABOVE'),
]

# ========================================
# GEMINI THINKING BUDGET (FIX — Aug 18, 2026 debugging session)
# ========================================
# ROOT CAUSE FOUND: isolated testing directly in Google AI Studio
# Playground (same account/project, zero app code involved) showed
# gemini-3.7-flash reliably failing ("An internal error has occurred")
# specifically when its Thinking level was left at the default
# "Medium" — a 34.1s request that errored out. The EXACT same prompt,
# same model, with Thinking level manually lowered to "Low" (or with
# gemini-2.5-flash using a small manual thinking budget of ~2000
# tokens) succeeded cleanly and repeatably (9s and 20.8s respectively).
# This isolates the failure to Google's backend struggling with
# extended/default thinking budgets on these models right now — NOT a
# bug in this app's code, NOT a quota/billing issue (2.5-flash worked
# fine throughout), and NOT related to which API surface is used.
#
# FIX: cap the thinking budget on every Gemini call in this file to a
# small, known-stable value instead of leaving it at the model's
# default (which behaves like "Medium" and triggers the failure).
# 2000 tokens is enough for the model to briefly reason about
# structuring the podcast script / mind map JSON without tipping into
# the failure zone observed above.
GEMINI_THINKING_BUDGET = int(os.environ.get('GEMINI_THINKING_BUDGET', '2000'))


def parse_device_info(user_agent):
    """Turns a raw User-Agent string into a short, readable summary like
    'Samsung Internet · Android · Mobile' or 'Chrome · Windows · Desktop'."""
    if not user_agent:
        return 'Unknown'

    ua = user_agent

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


def split_into_clauses(sentence, max_words=20):
    # FIX (Aug 18, 2026 — 45-52s gTTS slowness, confirmed via logs):
    # raised from 14. Render's Google TTS calls run ~4-8x slower than
    # they would from a home computer (confirmed: 1.67s avg per call in
    # production logs, vs a normal ~0.2-0.4s) — likely Google
    # throttling/deprioritizing traffic from cloud datacenter IPs. Each
    # clause is one separate network call, so fewer/longer clauses
    # means fewer total calls. Kept at 20 (not more) rather than
    # removing clause-splitting altogether, since natural mid-sentence
    # breathing pauses (the whole reason clauses exist) still matter
    # for audio quality — this is a moderate reduction in call count,
    # not a quality-for-speed trade that guts the pacing.
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
                             clause_pause_ms=140, forced_break_pause_ms=90, max_workers=15):
    # FIX (Aug 19, 2026 — REAL root cause, confirmed via production
    # logs): the Aug 18 fix (raising max_workers 5->15) was based on
    # the wrong theory. Logs from a real request showed 27 clause-level
    # calls taking 48.0s at a consistent ~1.78s/call — virtually
    # IDENTICAL to the 48.3s/1.67s-per-call seen BEFORE that fix. That
    # proves the calls were never actually bottlenecked by client-side
    # concurrency (5 vs 15 workers) — Google (or the network path
    # between Render and Google) appears to rate-limit/serialize
    # responses to this endpoint per-source-IP regardless of how many
    # requests fire in parallel. Raising max_workers changed nothing
    # because the calls weren't queueing on OUR side to begin with.
    #
    # The only lever left that actually reduces wall time is reducing
    # the NUMBER of calls. This function now makes ONE Google TTS call
    # per SENTENCE (not per clause) — cutting a typical note from ~27
    # calls down to roughly 8-10. Google's own TTS engine already
    # pauses naturally at commas/punctuation within a single spoken
    # request, so sentences still sound reasonably paced even without
    # our own explicit per-clause silence insertion — the trade-off is
    # slightly less fine-tuned breathing pauses in exchange for
    # roughly a 3x cut in total generation time.
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()] or [text]

    flat_sentences = []
    for p_index, paragraph in enumerate(paragraphs):
        sentences = split_into_sentences(paragraph) or [paragraph]
        for s_index, sentence in enumerate(sentences):
            is_last_in_paragraph = (s_index == len(sentences) - 1)
            flat_sentences.append((sentence, is_last_in_paragraph))
    if not flat_sentences:
        flat_sentences = [(text, True)]

    tasks = [(sentence_text, lang) for (sentence_text, _) in flat_sentences]

    # TEMP DIAGNOSTIC LOGGING (kept from Aug 18 — confirms the fix):
    # now counts SENTENCE calls, not clause calls. Compare this run's
    # total time against the ~48s baseline to confirm the improvement.
    _gtts_network_start = time.time()
    print(f"⏱️ gTTS: starting {len(tasks)} parallel SENTENCE calls to Google (max_workers={max_workers})...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        segments = list(executor.map(_tts_sentence_to_segment, tasks))

    _gtts_network_elapsed = time.time() - _gtts_network_start
    print(f"⏱️ gTTS: all {len(tasks)} sentence calls finished in {_gtts_network_elapsed:.1f}s (avg {_gtts_network_elapsed / max(1, len(tasks)):.2f}s/call)")

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
    'si': 'Leda',
    'ta': 'Leda',
    'en': 'Leda',
}

GEMINI_TTS_VOICE_OPTIONS = {
    'Sadaltager': 'Male',
    'Leda': 'Female',
}

GEMINI_TTS_MODEL_VERSIONS = {
    'v25': 'gemini-2.5-flash-preview-tts',
    'v31': 'gemini-3.1-flash-tts-preview',
}


def calculate_gemini_tts_coin_cost(text_length, model_version='v25'):
    if text_length <= 500:
        base = 5
    elif text_length <= 1200:
        base = 12
    else:
        base = 20
    return base * 2 if model_version == 'v31' else base


_gemini_tts_call_times = deque()
_gemini_tts_gate_lock = threading.Lock()
GEMINI_TTS_SAFE_RPM_PER_WORKER = 4
GEMINI_TTS_MAX_QUEUE_WAIT_SECONDS = 10


def _gemini_tts_call_gate():
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
    with _gemini_tts_gate_lock:
        _gemini_tts_call_times.append(time.time())


def synthesize_gemini_tts(text, lang='si', voice_name=None, model_version='v25'):
    if not client:
        raise GeminiGenerationError("Gemini API is not configured (missing GEMINI_API_KEY).")

    if voice_name not in GEMINI_TTS_VOICE_OPTIONS:
        voice_name = GEMINI_TTS_VOICES.get(lang, 'Leda')

    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()] or [text]
    flat_sentences = []
    for paragraph in paragraphs:
        sentences = split_into_sentences(paragraph) or [paragraph]
        flat_sentences.extend(sentences)
    if not flat_sentences:
        flat_sentences = [text]

    directed_prompt = (
        "Style: Empathetic — warm, caring, and encouraging delivery, "
        "like a supportive teacher patiently explaining something to a student. "
        "IMPORTANT: speak clearly and audibly at a normal, full speaking volume — "
        "NOT a whisper, NOT hushed or breathy, every word should be crisp and easy to hear.\n"
        "Pace: Natural, comfortable speaking speed — not rushed, not slow.\n\n"
        f"{text}"
    )

    # FIX (Aug 19, 2026 — automatic model fallback, same pattern as
    # call_gemini_structured): gemini-3.1-flash-tts-preview is a newer,
    # less-established preview model that's been showing repeated
    # 503/504 errors (confirmed both in our own production logs and on
    # Google's own developer forum — other developers report the exact
    # same "3.1 fails, 3.0/2.5-generation models go through" pattern).
    # Rather than failing outright when 3.1 is degraded, this now also
    # tries the more established 2.5 Flash TTS model — which sits on
    # separate capacity/infrastructure and is often still healthy even
    # when 3.1 isn't. Only falls back v31 -> v25 (never the reverse,
    # since v25 is the safer default already).
    model_attempt_plan = [model_version] if model_version == 'v25' else [model_version, 'v25']

    # FIX #2 (Aug 19, 2026 — confirmed by production logs): the first
    # UPDATE (Aug 19, 2026 — per explicit request): give BOTH models 2
    # attempts each (2+2) instead of the previous fail-fast-then-full-
    # retry split (1+3). Worst-case total time is still bounded by
    # TOTAL_TIME_BUDGET_SECONDS below, which aborts early if the
    # combined attempts run long — so this can't blow past gunicorn's
    # 180s worker timeout even if every attempt times out.
    def _max_attempts_for(is_last_model_in_plan):
        return 2

    response = None
    last_tts_error = None
    synth_start_time = time.time()
    # Raised from 150s now that gunicorn's own worker timeout is 180s
    # (see Render Start Command) — gives the reliable fallback model
    # genuine room for its attempts instead of running out of budget
    # immediately after the first model gives up.
    TOTAL_TIME_BUDGET_SECONDS = 165
    actual_model_used = model_version

    for plan_index, plan_model in enumerate(model_attempt_plan):
        is_last_model = (plan_index == len(model_attempt_plan) - 1)
        max_attempts_this_model = _max_attempts_for(is_last_model)
        model_succeeded = False
        for attempt in range(1, max_attempts_this_model + 1):
            elapsed = time.time() - synth_start_time
            if elapsed > TOTAL_TIME_BUDGET_SECONDS:
                print(f"⚠️ Gemini TTS aborting — total time budget ({TOTAL_TIME_BUDGET_SECONDS}s) exceeded after {elapsed:.1f}s")
                raise GeminiGenerationError("Gemini TTS timed out across retries (server-side time budget exceeded).")

            _gemini_tts_call_gate()
            try:
                response = client.models.generate_content(
                    model=GEMINI_TTS_MODEL_VERSIONS.get(plan_model, GEMINI_TTS_MODEL_VERSIONS['v25']),
                    contents=directed_prompt,
                    config=types.GenerateContentConfig(
                        response_modalities=["AUDIO"],
                        speech_config=types.SpeechConfig(
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                            )
                        ),
                        http_options=types.HttpOptions(timeout=40_000),  # milliseconds — see timeout/retry note above
                    )
                )
                actual_model_used = plan_model
                model_succeeded = True
                if plan_model != model_version:
                    print(f"✅ Gemini TTS succeeded on fallback model {plan_model} (requested {model_version} was unavailable)")
                break  # success — exit retry loop for this model
            except Exception as e:
                last_tts_error = e
                if _is_rate_limit_error(e) and attempt < max_attempts_this_model:
                    print(f"⚠️ Gemini TTS ({plan_model}) rate-limited (attempt {attempt}), retrying in 5s: {e}")
                    time.sleep(5)
                    continue
                if _is_transient_gemini_error(e) and attempt < max_attempts_this_model:
                    wait_seconds = min(attempt * 3, 8)
                    print(f"⚠️ Gemini TTS ({plan_model}) transient error (attempt {attempt}), retrying in {wait_seconds}s: {e}")
                    time.sleep(wait_seconds)
                    continue
                # Non-retryable error, or retries exhausted for this model
                print(f"⚠️ Gemini TTS ({plan_model}) failed after {attempt} attempt(s): {e}")
                break  # give up on this model, try the next one in the plan (if any)
        if model_succeeded:
            break  # got audio — no need to try the next model in the plan

    if response is None:
        raise last_tts_error or GeminiGenerationError("Gemini TTS failed after retries.")

    if not getattr(response, 'candidates', None):
        raise GeminiGenerationError("Gemini TTS returned no audio (possibly blocked by safety filters).")

    audio_data = response.candidates[0].content.parts[0].inline_data.data
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

    return audio_segment, sentence_timings, actual_model_used


class OpenAIGenerationError(Exception):
    pass


# Two voices offered — a clear Male and Female choice, matching the
# same simplified pattern used for Gemini's voice picker (2 options,
# not OpenAI's full list of 9+) so students aren't overwhelmed.
OPENAI_TTS_VOICE_OPTIONS = {
    'fable': 'Male',
    'nova': 'Female',
}
OPENAI_TTS_MODEL = 'gpt-4o-mini-tts'  # OpenAI's newest, most reliable TTS model (GA, not preview) — confirmed working with Sinhala in the OpenAI Playground


def calculate_openai_tts_coin_cost(text_length):
    """Same tiered-pricing shape as the Gemini v25 tiers — OpenAI's
    real per-character cost for gpt-4o-mini-tts sits in a broadly
    similar range, so reusing the same tier boundaries keeps pricing
    predictable and easy to explain to students, rather than
    introducing a third, different-looking price table."""
    if text_length <= 500:
        return 5
    elif text_length <= 1200:
        return 12
    else:
        return 20


def _openai_tts_sentence_to_segment(args):
    sentence, voice_name = args
    response = openai_client.audio.speech.create(
        model=OPENAI_TTS_MODEL,
        voice=voice_name,
        input=sentence,
        response_format='mp3',
    )
    audio_bytes = response.content
    segment = AudioSegment.from_file(io.BytesIO(audio_bytes), format='mp3')
    return segment.set_frame_rate(GTTS_FRAME_RATE).set_channels(1)


def synthesize_openai_tts(text, voice_name='nova', max_workers=8):
    """Generates natural-sounding audio using OpenAI's TTS API — a GA
    (production-stable, not preview) alternative Natural Voice engine
    to Gemini TTS. Sentence-level parallel calls, same pattern proven
    to work well for gTTS (see synthesize_gtts_natural above) — OpenAI
    doesn't return per-sentence timing either, so timings are measured
    the same way, from each sentence's own actual audio duration."""
    if not openai_client:
        raise OpenAIGenerationError("OpenAI API is not configured (missing OPENAI_API_KEY).")

    if voice_name not in OPENAI_TTS_VOICE_OPTIONS:
        voice_name = 'nova'

    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()] or [text]
    flat_sentences = []
    for paragraph in paragraphs:
        sentences = split_into_sentences(paragraph) or [paragraph]
        flat_sentences.extend(sentences)
    if not flat_sentences:
        flat_sentences = [text]

    tasks = [(sentence_text, voice_name) for sentence_text in flat_sentences]

    print(f"⏱️ OpenAI TTS: starting {len(tasks)} parallel sentence calls (voice={voice_name})...")
    _start = time.time()

    max_retries = 2
    last_error = None
    segments = None
    for attempt in range(1, max_retries + 1):
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                segments = list(executor.map(_openai_tts_sentence_to_segment, tasks))
            break
        except Exception as e:
            last_error = e
            print(f"⚠️ OpenAI TTS attempt {attempt} failed: {e}")
            if attempt < max_retries:
                time.sleep(3)
                continue
            raise OpenAIGenerationError(f"OpenAI TTS failed after {max_retries} attempts: {e}")

    print(f"⏱️ OpenAI TTS: all {len(tasks)} calls finished in {time.time() - _start:.1f}s")

    pause_ms = 350
    paragraph_pause_ms = 600
    sentence_silence = AudioSegment.silent(duration=pause_ms, frame_rate=GTTS_FRAME_RATE)
    combined = AudioSegment.silent(duration=0, frame_rate=GTTS_FRAME_RATE)

    sentence_timings = []
    total = len(flat_sentences)
    for i, (sentence_text, segment) in enumerate(zip(flat_sentences, segments)):
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
        combined += sentence_silence

    return combined, sentence_timings


class GeminiGenerationError(Exception):
    pass


def build_system_instruction(output_language):
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


def _parse_json_loose(raw_text, error_cls=GeminiGenerationError):
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
        raise error_cls("Response could not be parsed as JSON.")


def _sanitize_mermaid_labels(code):
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
    # FIX (Aug 19, 2026 — confirmed by production logs): "TIMEOUT" (one
    # word) never matches Python/httpx's actual client-side timeout
    # message, which reads "The read operation timed out" (two words,
    # past tense) — so genuine network timeouts were silently treated
    # as NON-retryable and immediately failed the whole request,
    # skipping the retry logic entirely. This was invisible until now
    # because Google's own server-side errors (504 DEADLINE_EXCEEDED,
    # 503 UNAVAILABLE) DID match and retried fine — only the client-side
    # "gave up waiting for a response" case fell through the gap.
    msg = str(exc).upper()
    transient_markers = [
        '500', '502', '503', '504', 'INTERNAL', 'UNAVAILABLE',
        'TIMEOUT', 'TIMED OUT', 'DEADLINE_EXCEEDED',
        'CONNECTION RESET', 'CONNECTION ABORTED', 'READ OPERATION',
    ]
    return any(marker in msg for marker in transient_markers)


def _friendly_gemini_error_message(exc):
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
    generation."""
    if not client:
        raise GeminiGenerationError("Gemini API is not configured (missing GEMINI_API_KEY).")

    prompt = f"පහත content එකෙන් quiz එකක් හදන්න:\n\n{content_text}"

    last_error = None
    quiz_start_time = time.time()
    # FIX (Aug 18, 2026 — see GEMINI_THINKING_BUDGET note above): bumped
    # from 45s. With thinking capped at GEMINI_THINKING_BUDGET, calls
    # are stable but can still legitimately take 20-30s+ (confirmed via
    # AI Studio Playground testing) — the OLD 45s budget only really
    # allowed for one full attempt at that length before aborting.
    QUIZ_TIME_BUDGET_SECONDS = 75  # still safely under gunicorn's 120s worker timeout

    QUIZ_MODELS_TO_TRY = ['gemini-3.7-flash', 'gemini-2.5-flash']
    quiz_attempt_plan = [(model, n) for model in QUIZ_MODELS_TO_TRY for n in range(1, 2 + 1)]

    response = None
    for plan_index, (model_name, attempt) in enumerate(quiz_attempt_plan, start=1):
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
                    # FIX (Aug 18, 2026): isolated Playground testing
                    # showed Gemini 3.7/2.5-flash reliably erroring out
                    # ("An internal error has occurred") specifically at
                    # default/Medium thinking levels, and reliably
                    # succeeding once the thinking budget was capped —
                    # see GEMINI_THINKING_BUDGET comment above for the
                    # full writeup. This was NOT an app-code bug.
                    thinking_config=types.ThinkingConfig(thinking_budget=GEMINI_THINKING_BUDGET),
                    # FIX: raised from 15s — that was cutting off
                    # legitimate in-progress responses (confirmed
                    # successful Playground responses took 9-21s even
                    # with the thinking budget capped) before Gemini
                    # ever got a chance to finish.
                    http_options=types.HttpOptions(timeout=30_000),  # milliseconds
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
    # FIX (Aug 18, 2026 — root-caused via AI Studio Playground testing,
    # see GEMINI_THINKING_BUDGET comment above): bumped from 45s. Now
    # that thinking is capped (stable, no more internal-error/timeouts
    # at the model level), calls can still legitimately run 20-30s+
    # end-to-end — the old 45s total budget across 4 attempts left very
    # little room. 90s stays safely under gunicorn's 120s worker
    # timeout while giving real attempts room to actually finish.
    TOTAL_TIME_BUDGET_SECONDS = 90

    # FIX (Aug 26, 2026 — production logs showed gemini-3.7-flash
    # consistently 503/504-ing under current high demand, EVERY single
    # request, often eating its full 40s timeout before falling back —
    # while gemini-2.5-flash succeeded immediately every single time.
    # Swapped order so the model that's actually reliable right now is
    # tried first — 3.7-flash still gets a shot as the fallback, just
    # no longer sits in the critical path of every request's latency.
    MODELS_TO_TRY = ['gemini-2.5-flash', 'gemini-3.7-flash']
    # Dropped from 2 to 1 (see fix above): a failure now falls through
    # to the next model right away instead of hammering the same
    # overloaded one twice first.
    ATTEMPTS_PER_MODEL = 1
    attempt_plan = [(model, n) for model in MODELS_TO_TRY for n in range(1, ATTEMPTS_PER_MODEL + 1)]

    response = None
    for plan_index, (model_name, attempt) in enumerate(attempt_plan, start=1):
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
                    # FIX (Aug 18, 2026 — ROOT CAUSE of the Smart Study
                    # "An internal error has occurred" failures): direct
                    # testing in Google AI Studio Playground (same
                    # account/project, zero app code involved) isolated
                    # this to Gemini 3.7-flash/2.5-flash reliably
                    # erroring out at their default/"Medium" thinking
                    # level, and reliably succeeding once the thinking
                    # budget was capped low. Full writeup is on the
                    # GEMINI_THINKING_BUDGET constant near the top of
                    # this file.
                    thinking_config=types.ThinkingConfig(thinking_budget=GEMINI_THINKING_BUDGET),
                    # FIX: raised from 15s — Playground confirmed
                    # successful responses (even with thinking capped)
                    # legitimately took up to ~21-34s; 15s was cutting
                    # real, in-progress responses off before Gemini
                    # finished.
                    http_options=types.HttpOptions(timeout=40_000),  # milliseconds
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
                wait_seconds = 2
                print(f"⚠️ {model_name} transient error (attempt {attempt}/{ATTEMPTS_PER_MODEL}), retrying/switching in {wait_seconds}s: {e}")
                time.sleep(wait_seconds)
                continue
            if is_last_plan_step:
                raise GeminiGenerationError(f"Gemini request failed after trying {MODELS_TO_TRY}: {last_error}")
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


OPENAI_SCRIPT_MODEL = 'gpt-4o-mini'  # fallback script/mind-map generator when Gemini fails — cheap, JSON-mode capable


def call_openai_structured(note_text, output_language='si'):
    """Fallback for call_gemini_structured — same JSON contract
    (podcast_script/mermaid_code_si/mermaid_code_en) and same system
    instruction, but via OpenAI. Only called from /process-note AFTER
    Gemini has already exhausted its own model/retry plan (up to 90s),
    so this keeps its own retry budget short to stay under gunicorn's
    120s worker timeout."""
    if not openai_client:
        raise OpenAIGenerationError("OpenAI API is not configured (missing OPENAI_API_KEY).")

    prompt = f"""පහත පාඩම් සටහන සකසන්න:

{note_text}
"""
    system_instruction = build_system_instruction(output_language)

    last_error = None
    response = None
    for attempt in (1, 2):
        try:
            response = openai_client.chat.completions.create(
                model=OPENAI_SCRIPT_MODEL,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=8000,
                response_format={"type": "json_object"},
                timeout=25,
            )
            break
        except Exception as e:
            last_error = e
            if attempt == 1 and _is_transient_gemini_error(e):
                print(f"⚠️ OpenAI script fallback transient error, retrying once: {e}")
                time.sleep(2)
                continue
            raise OpenAIGenerationError(f"OpenAI script generation failed: {last_error}")

    if response is None:
        raise OpenAIGenerationError(f"OpenAI script generation failed: {last_error}")

    raw_text = (response.choices[0].message.content or '').strip()
    if not raw_text:
        raise OpenAIGenerationError("OpenAI returned an empty response.")

    data = _parse_json_loose(raw_text, error_cls=OpenAIGenerationError)

    podcast_script = _clean_podcast_script((data.get('podcast_script') or '').strip())
    mermaid_code_si = _clean_mermaid_code(data.get('mermaid_code_si'))
    mermaid_code_en = _clean_mermaid_code(data.get('mermaid_code_en'))

    if not podcast_script:
        raise OpenAIGenerationError("OpenAI did not return a podcast script.")

    return podcast_script, mermaid_code_si, mermaid_code_en


# ========================================
# DIGITAL TUITION SIR — course content pipeline
# (admin-authored PDF -> day-by-day lessons -> teacher-style script ->
# audio, all generated ONCE per course and served to every enrolled
# student, unlike the per-user note pipeline above)
# ========================================

TEACHER_SYSTEM_INSTRUCTION = """
ඔබ ශ්‍රී ලංකාවේ ඉතා ප්‍රසිද්ධ, ළමයින්ට ප්‍රියතම Tuition Sir කෙනෙකි. ඔබට textbook එකේ
lesson කොටසක් ලැබී ඇත. ඔබ එය **පන්තියක සිටින ළමයෙකුට කෙළින්ම කතා කරමින්** උගන්වන
ආකාරයට podcast script එකක් බවට පත් කරන්න.

**JSON object එකක් විතරක්** output කරන්න, වෙන කිසිදු text/markdown fence නොදාන්න:
{
  "podcast_script": "<සම්පූර්ණ teaching script එක>",
  "mermaid_code_si": "<Mermaid.js mindmap syntax එකක්, node labels සියල්ලම සිංහලෙන්>",
  "mermaid_code_en": "<Mermaid.js mindmap syntax එකක්, node labels සියල්ලම English වලින්>"
}

=== "podcast_script" සඳහා නීති ===
1. **Hook** — "ළමයිනේ, අද අපි ඉගෙන ගන්නවා..." වගේ, topic එක හඳුන්වා දෙන්නෙන් පටන් ගන්න.
2. **Real-life connect** — content එකේ raw fact එකකින් නෙවෙයි, ළමයෙකුට familiar
   example එකකින් පටන් ගන්න.
3. **Step-by-step explain** — fact එක කෙළින්ම කියවන්නේ නැතුව "ඇයි මෙහෙම" කියලා
   logic එකෙන් walk-through කරන්න.
4. **Check-in questions** — "තේරුණාද මේක? හරි, ඊළඟට..." වගේ, engage කරන්න.
5. **Recap** — අන්තිමට "ඉතින් අද ඉගෙන ගත්තේ මොනවද කිව්වොත්..." කියලා සාරාංශ කරන්න.
6. Content එකේ ඇති සියලුම facts, terms, numbers කිසිසේත් මගහැරෙන්නේ නෑ.
7. සරල, ස්වාභාවික, කථනාත්මක සිංහල භාෂාවෙන්ම ලියන්න.
8. **ඉතාම වැදගත් — මේක TEXT-TO-SPEECH එකකින් ශබ්දයෙන් කියවෙනවා, කියවන්නේ නෑ:**
   කිසිසේත් markdown formatting (**, ##, -, *) හෝ numbered lists (1. 2. 3.) යොදන්න
   එපා — TTS එකෙන් ඒ symbols ම වචන විදිහට කියවෙනවා, එය අස්වාභාවික හඬ නගනවා.
   "පළමුවෙනුව", "ඊළඟට", "දෙවෙනුව", "අන්තිමට" වගේ කථනාත්මක වචන පාවිච්චි කරලා list
   එකක් තියෙන බව පෙන්වන්න. සම්පූර්ණ script එකම plain, running Sinhala වාක්‍ය පමණක් විය
   යුතුය.
9. "podcast_script" එක අකුරු 1800ක් නොඉක්මවිය යුතුය.
10. **ඉතා වැදගත් — එකම නම/වචනය දෙපාරක් නොකියවෙන්න:** Source content එකේ scientific/
    technical නම් (මූලද්‍රව්‍ය, brand names, ආදිය) සිංහල phonetic spelling එකක් සහ
    English/Latin මුල් නම දෙකම වරහන් තුළ එකට තිබුණොත් (උදා: "හයිඩ්‍රාජිරම්
    (hydrargyrum)"), **එකක් විතරක් තෝරන්න** — TTS එකෙන් දෙකම කියෙව්වොත් එකම නම
    දෙපාරක් කියවෙනවා වගේ අස්වාභාවික හඬක් නගනවා. සාමාන්‍යයෙන් ළමයින්ට වඩාත් හුරු/
    කියවන්න ලේසි එක තෝරන්න (බොහෝවිට සිංහල phonetic එකම).

=== "mermaid_code_si"/"mermaid_code_en" සඳහා නීති ===
- "flowchart TD" වලින්ම පටන් ගන්න.
- Root node එකෙන් කෙලින්ම පටන්ගන්නා ප්‍රධාන branch ගණන උපරිම 4කට සීමා කරන්න.
- Node id ලෙස ඉංග්‍රීසි අකුරු/සංඛ්‍යා පමණක් (Sinhala නෑ), දෙකෙහිම node id එකම විය යුතුය.
"""


def call_teacher_script(source_text, max_retries=2):
    """Turns a lesson's raw (verbatim-from-PDF) source_text into a
    warm, spoken "Sir teaching a class" script + mind map — the
    Digital Tuition Sir equivalent of call_gemini_structured above,
    but with a teaching-persona prompt instead of a generic podcast
    one, and no output_language branch (course lessons are Sinhala-
    only for now)."""
    if not client:
        raise GeminiGenerationError("Gemini API is not configured (missing GEMINI_API_KEY).")

    MODELS_TO_TRY = ['gemini-2.5-flash', 'gemini-3.7-flash']
    last_error = None
    response = None
    for model_name in MODELS_TO_TRY:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=f"මේ lesson එක teach කරන්න:\n\n{source_text}",
                config=types.GenerateContentConfig(
                    system_instruction=TEACHER_SYSTEM_INSTRUCTION,
                    temperature=0.4,
                    max_output_tokens=8000,
                    response_mime_type='application/json',
                    safety_settings=SAFETY_SETTINGS,
                    thinking_config=types.ThinkingConfig(thinking_budget=GEMINI_THINKING_BUDGET),
                    http_options=types.HttpOptions(timeout=40_000),
                )
            )
            break
        except Exception as e:
            last_error = e
            if _is_transient_gemini_error(e):
                continue
            raise GeminiGenerationError(f"Teacher script generation failed: {e}")

    if response is None:
        raise GeminiGenerationError(f"Teacher script generation failed after trying {MODELS_TO_TRY}: {last_error}")

    raw_text = response.text
    if not raw_text:
        raise GeminiGenerationError("Gemini returned an empty teacher-script response.")

    data = _parse_json_loose(raw_text)
    script = _clean_podcast_script((data.get('podcast_script') or '').strip())
    mermaid_si = _clean_mermaid_code(data.get('mermaid_code_si'))
    mermaid_en = _clean_mermaid_code(data.get('mermaid_code_en'))

    # Safety net (defense in depth): strip any stray markdown the model
    # might still slip in, since a TTS engine reads "**"/"1." literally.
    script = re.sub(r'\*\*?|#+', '', script)
    script = re.sub(r'(?:^|\n)\s*\d+\.\s*', ' ', script)
    script = re.sub(r'\s+', ' ', script).strip()

    if not script:
        raise GeminiGenerationError("Gemini did not return a teaching script.")

    return script, mermaid_si, mermaid_en


def build_split_system_instruction(min_words, max_words):
    return f"""
ඔබ ශ්‍රී ලංකාවේ පළපුරුදු Tuition Sir කෙනෙකි. ඔබට textbook/lesson content එකක් සම්පූර්ණයෙන්ම
ලැබී ඇත. ඔබේ වැඩේ මේක **day-by-day lessons** කිහිපයකට කඩන එකයි — ශිෂ්‍යයෙකුට දිනකට එක
lesson එකක් විදිහට ඉගෙන ගන්න පුළුවන් වෙන ලෙස.

**JSON object එකක් විතරක්** output කරන්න, වෙන කිසිදු text/markdown fence නොදාන්න:
{{
  "lessons": [
    {{ "title": "<මේ lesson එකේ මාතෘකාව, වචන 3-6ක් තරම්>", "source_text": "<මුල් content එකෙන්ම>" }}
  ]
}}

=== නීති ===
1. **source_text එක මුල් text එකෙන්ම විය යුතුය** — සාරාංශ කරන්න එපා, rewrite කරන්න එපා,
   වචනයක්වත් වෙනස් කරන්න එපා.
2. එක lesson එකක් තුළ **එක logical topic/sub-topic කණ්ඩායමක්** විතරක් තියෙන්න ඕන —
   මැද්දෙන් definition එකක් හෝ example එකක් කැඩෙන්නේ නැති ලෙස boundary එක තෝරන්න.
3. එක lesson එකක source_text එකේ දිග වචන {min_words}-{max_words} අතර තියෙන්න උත්සාහ කරන්න
   (හරියටම නෙවෙයි — topic boundary එකට priority දෙන්න, word count එකට නෙවෙයි).
4. Lessons පිළිවෙළ, මුල් content එකේ පිළිවෙළම විය යුතුය.
5. OCR text එකේ ඇති page numbers, headers වගේ noise (topic content නොවන දේ) lessons
   වලට ඇතුළත් කරන්න එපා.
6. content එකේ ඇති සියලුම කොටස්ම එක lesson එකකට හෝ තව එකකට allocate විය යුතුය —
   කිසිම කොටසක් මගහැරෙන්නේ නෑ, duplicate වෙන්නේත් නෑ.
"""


def _text_to_chunks(full_text, words_per_chunk=2400):
    """Splits OCR'd text into ~words_per_chunk-sized pieces on paragraph
    boundaries. Deterministic given the same input, so a step-based job
    can recompute chunk N on demand instead of having to persist the
    whole chunk list separately from full_text."""
    paragraphs = [p for p in full_text.split('\n\n') if p.strip()]
    chunks = []
    current, current_words = [], 0
    for para in paragraphs:
        para_words = len(para.split())
        if current and current_words + para_words > words_per_chunk:
            chunks.append('\n\n'.join(current))
            current, current_words = [], 0
        current.append(para)
        current_words += para_words
    if current:
        chunks.append('\n\n'.join(current))
    return chunks


def _split_one_gemini_chunk(chunk, split_instruction):
    """One chunk's worth of the PDF-split Gemini call. Kept as a
    standalone, single synchronous call (not batched, not threaded) —
    see the FIX note on the pdf-job routes below for why."""
    if not client:
        raise GeminiGenerationError("Gemini API is not configured (missing GEMINI_API_KEY).")

    last_error = None
    response = None
    for model_name in ['gemini-2.5-flash', 'gemini-3.7-flash']:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=f"මේ content එක lessons වලට කඩන්න:\n\n{chunk}",
                config=types.GenerateContentConfig(
                    system_instruction=split_instruction,
                    temperature=0.2,
                    max_output_tokens=16000,
                    response_mime_type='application/json',
                    safety_settings=SAFETY_SETTINGS,
                    thinking_config=types.ThinkingConfig(thinking_budget=GEMINI_THINKING_BUDGET),
                    http_options=types.HttpOptions(timeout=60_000),
                )
            )
            break
        except Exception as e:
            last_error = e
            if _is_transient_gemini_error(e):
                continue
            raise GeminiGenerationError(f"PDF split failed on one chunk: {e}")
    if response is None:
        raise GeminiGenerationError(f"PDF split failed on one chunk after retries: {last_error}")
    data = _parse_json_loose(response.text)
    return data.get('lessons') or []


LESSON_AUDIO_DIR = os.path.join('static', 'lesson_audio')  # local fallback only — see upload_persistent_audio() below


def upload_persistent_audio(audio_bytes, public_id):
    """Stores lesson audio somewhere that survives a redeploy. Uploads
    to Cloudinary when configured (production); falls back to local
    disk under LESSON_AUDIO_DIR for local dev/testing without a
    Cloudinary account (that path is NOT durable on Render — see the
    CLOUDINARY_AVAILABLE warning printed at startup). Returns the
    servable URL either way."""
    if CLOUDINARY_AVAILABLE:
        result = cloudinary.uploader.upload(
            io.BytesIO(audio_bytes),
            resource_type='video',  # Cloudinary files audio under 'video'
            public_id=public_id,
            folder='lesson_audio',
            overwrite=True,
        )
        return result['secure_url']

    os.makedirs(LESSON_AUDIO_DIR, exist_ok=True)
    filepath = os.path.join(LESSON_AUDIO_DIR, f'{public_id}.mp3')
    with open(filepath, 'wb') as f:
        f.write(audio_bytes)
    return '/' + filepath.replace('\\', '/')


def generate_lesson_audio(script_text):
    """Cascading fallback for course-lesson audio, same resilience
    order proven out manually for this feature: Gemini 3.1 -> Gemini
    2.5 (already Gemini's own internal fallback inside
    synthesize_gemini_tts) -> OpenAI 2.1 -> gTTS. A lesson's audio
    should never be blocked entirely by one provider being down.
    Returns (audio_segment, engine_used, sentence_timings) — timings
    power the student player's word-by-word highlight (same feature
    the quick-note flow already has)."""
    formatted = format_for_podcast(script_text)
    lang = detect_language(formatted)
    gtts_lang = {'Sinhala': 'si', 'Tamil': 'ta', 'English': 'en'}.get(lang, 'si')

    if client:
        try:
            audio_segment, timings, actual_model_used = synthesize_gemini_tts(formatted, lang=gtts_lang, model_version='v31')
            return audio_segment, f'gemini-{actual_model_used}', timings
        except Exception as e:
            print(f"⚠️ Lesson audio: Gemini TTS unavailable, trying OpenAI: {e}")

    if openai_client:
        try:
            audio_segment, timings = synthesize_openai_tts(formatted, voice_name='nova')
            return audio_segment, 'openai-v21', timings
        except Exception as e:
            print(f"⚠️ Lesson audio: OpenAI TTS unavailable, falling back to gTTS: {e}")

    audio_segment, timings = synthesize_gtts_natural(formatted, lang=gtts_lang)
    return audio_segment, 'gtts', timings


def _ocr_pdf_pages_to_text(pdf_bytes, start_page=0, max_pages=80):
    """Same render-page-as-image + Cloud Vision OCR approach as
    /pdf-extract below — factored out so the course-PDF-split admin
    flow can reuse it with a higher page cap (curated admin uploads,
    not a public abuse-prone endpoint) AND process a big PDF in
    page-range batches (start_page lets the caller OCR e.g. pages
    30-59 on one call) rather than always starting from page 0."""
    if not CLOUD_OCR_AVAILABLE:
        raise RuntimeError("Cloud Vision API not configured")

    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    total_pages_in_pdf = len(doc)
    end_page = min(total_pages_in_pdf, start_page + max_pages)
    pages_text = []
    for i in range(start_page, end_page):
        page = doc[i]
        # FIX (Aug 27, 2026): dropped from 2x zoom — Render's free tier
        # (512MB total) was OOM-killing the whole instance mid-job.
        # 1.5x is still comfortably sharp for Vision OCR on printed
        # textbook text and meaningfully cuts per-page image memory
        # (pixmap + PNG encode), which matters here since this loop can
        # run for many pages across a job. Also drop the pixmap/bytes
        # references explicitly once OCR'd instead of waiting for the
        # next loop iteration to overwrite them.
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        img_bytes = pix.tobytes('png')
        pix = None
        image = vision.Image(content=img_bytes)
        img_bytes = None
        image_context = vision.ImageContext(language_hints=['si', 'ta', 'en'])
        response = vision_client.text_detection(image=image, image_context=image_context)
        if response.error.message:
            print(f"⚠️ Vision error on course PDF page {i + 1}: {response.error.message}")
            continue
        texts = response.text_annotations
        if texts:
            page_text = texts[0].description.strip()
            if page_text:
                pages_text.append(page_text)
    doc.close()
    return '\n\n'.join(pages_text).strip(), total_pages_in_pdf, end_page


@app.route('/pdf-extract', methods=['POST'])
@rate_limited(6, 60)
def pdf_extract():
    """Renders each PDF page as an IMAGE and runs it through the same
    Cloud Vision OCR used for photos, instead of reading the PDF's text
    layer directly."""
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
            # Dropped from 2x — see the same fix in _ocr_pdf_pages_to_text
            # above (Render's 512MB memory ceiling was OOM-killing the
            # whole instance under load).
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            img_bytes = pix.tobytes('png')
            pix = None

            image = vision.Image(content=img_bytes)
            img_bytes = None
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

        total_pages_in_pdf = len(doc)
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
@rate_limited(10, 60)
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
@rate_limited(10, 60)
def text_to_speech():
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    if _is_banned(anon_id=(data.get('anon_id') or '').strip()[:64], email=session.get('user_email')):
        return jsonify({'status': 'error', 'message': 'ඔබේ account/device එකට මේ feature එක restrict කර ඇත.'}), 403
    engine = (data.get('engine') or 'gtts').strip().lower()
    if engine not in ('gtts', 'gemini'):
        engine = 'gtts'
    voice_name = (data.get('voice_name') or '').strip()
    # NEW: model_version now spans BOTH providers — 'v21' means OpenAI
    # (gpt-4o-mini-tts, positioned/labeled "NoteWav 2.1" in the UI,
    # before "NoteWav 2.5"), 'v25'/'v31' mean Gemini as before. This
    # keeps a single "AI Model Version" picker in the UI covering all
    # three options, rather than a separate top-level engine choice.
    model_version = (data.get('model_version') or 'v25').strip()
    if model_version not in ('v21', 'v25', 'v31'):
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

    # Maps the Male/Female choice consistently across providers — the
    # frontend's voice picker sends Gemini-style names (Sadaltager =
    # Male, Leda = Female) regardless of which model_version is
    # selected, since it's the same conceptual choice either way.
    GEMINI_TO_OPENAI_VOICE = {'Sadaltager': 'fable', 'Leda': 'nova'}

    if engine == 'gemini':
        # NEW (Aug 19, 2026): "NoteWav 2.1" — OpenAI's gpt-4o-mini-tts,
        # a GA/stable alternative positioned before "NoteWav 2.5" in the
        # UI. Routed here (not a separate top-level engine) so it lives
        # in the same "AI Model Version" picker as the two Gemini
        # options, giving the person 3 models to choose from under one
        # "Natural (AI)" umbrella.
        if model_version == 'v21':
            _openai_start = time.time()
            try:
                openai_voice = GEMINI_TO_OPENAI_VOICE.get(voice_name, voice_name)
                if openai_voice not in OPENAI_TTS_VOICE_OPTIONS:
                    openai_voice = 'nova'
                print(f"🎙️ Requesting OpenAI TTS ({len(formatted_text)} chars, voice: {openai_voice})...")
                audio_segment, sentence_timings = synthesize_openai_tts(formatted_text, voice_name=openai_voice)
                unique_name = f"output_{uuid.uuid4().hex}.mp3"
                filename = os.path.join('static', unique_name)
                audio_segment.export(filename, format='mp3')
                _openai_elapsed = time.time() - _openai_start
                print(f"✅ OpenAI TTS Success! Total time: {_openai_elapsed:.1f}s")
                log_engine_event('tts', 'openai', 'v21', _openai_elapsed, success=True)
                return jsonify({
                    'status': 'success',
                    'audio_url': '/' + filename.replace('\\', '/'),
                    'sentence_timings': sentence_timings,
                    'engine': 'openai',
                    'coin_cost': calculate_openai_tts_coin_cost(len(formatted_text)),
                    'model_version': 'v21',
                    'fallback_used': False,
                })
            except Exception as e:
                print(f"❌ OpenAI TTS Error: {str(e)}")
                log_engine_event('tts', 'openai', 'v21', time.time() - _openai_start, success=False, error_message=str(e))
                return jsonify({
                    'status': 'error',
                    'message': 'NoteWav 2.1 එකෙන් audio එක generate කරගැනීම අසාර්ථක විය — වෙනත් AI Model එකකින් try කරන්න.'
                }), 500

        _gemini_tts_route_start = time.time()
        try:
            print(f"🎙️ Requesting Gemini TTS ({len(formatted_text)} chars, lang: {gtts_lang})...")
            audio_segment, sentence_timings, actual_model_used = synthesize_gemini_tts(formatted_text, lang=gtts_lang, voice_name=voice_name, model_version=model_version)
            unique_name = f"output_{uuid.uuid4().hex}.mp3"
            filename = os.path.join('static', unique_name)
            audio_segment.export(filename, format='mp3')
            _gemini_tts_elapsed = time.time() - _gemini_tts_route_start
            print(f"✅ Gemini TTS Success! (model used: {actual_model_used})")
            log_engine_event('tts', 'gemini', actual_model_used, _gemini_tts_elapsed, success=True)
            return jsonify({
                'status': 'success',
                'audio_url': '/' + filename.replace('\\', '/'),
                'sentence_timings': sentence_timings,
                'engine': 'gemini',
                # FIX (Aug 19, 2026): charge for whichever model ACTUALLY
                # generated the audio, not necessarily the one the person
                # picked — if v31 failed and this fell back to v25, the
                # person should pay v25's lower price, not v31's.
                'coin_cost': calculate_gemini_tts_coin_cost(len(formatted_text), model_version=actual_model_used),
                'model_version': actual_model_used,
                'fallback_used': actual_model_used != model_version,
            })
        except Exception as e:
            print(f"❌ Gemini TTS Error: {str(e)}")
            log_engine_event('tts', 'gemini', model_version, time.time() - _gemini_tts_route_start, success=False, error_message=str(e))
            return jsonify({
                'status': 'error',
                'message': 'NoteWav AI Voice (Beta) එකෙන් audio එක generate කරගැනීම අසාර්ථක විය — Standard Voice එකෙන් try කරන්න.'
            }), 500

    try:
        _tts_route_start = time.time()
        print(f"🎙️ Requesting gTTS ({len(formatted_text)} chars, lang: {gtts_lang})...")
        audio_segment, sentence_timings = synthesize_gtts_natural(formatted_text, lang=gtts_lang)
        unique_name = f"output_{uuid.uuid4().hex}.mp3"
        filename = os.path.join('static', unique_name)
        audio_segment.export(filename, format='mp3')
        _gtts_elapsed = time.time() - _tts_route_start
        print(f"✅ TTS Success! Total /tts route time: {_gtts_elapsed:.1f}s")
        log_engine_event('tts', 'gtts', None, _gtts_elapsed, success=True)
        return jsonify({
            'status': 'success',
            'audio_url': '/' + filename.replace('\\', '/'),
            'sentence_timings': sentence_timings,
            'engine': 'gtts',
        })
    except Exception as e:
        print(f"❌ gTTS Error: {str(e)}")
        log_engine_event('tts', 'gtts', None, time.time() - _tts_route_start, success=False, error_message=str(e))
        return jsonify({
            'status': 'error',
            'message': 'gTTS හරහා audio එක generate කරගැනීම අසාර්ථක විය. පසුව උත්සාහ කරන්න.'
        }), 500


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/.well-known/assetlinks.json')
def assetlinks_json():
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
        conn.execute(
            "UPDATE users SET email = ?, picture = ?, last_login = ? WHERE google_id = ?",
            (email, picture, now_iso, google_id)
        )
    else:
        conn.execute(
            """INSERT INTO users (google_id, email, name, picture, coins, streak, last_streak_date, created_at, last_login)
               VALUES (?, ?, ?, ?, 100, 0, NULL, ?, ?)""",
            (google_id, email, name, picture, now_iso, now_iso)
        )
    conn.commit()
    conn.close()

    session.permanent = True
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


@app.route('/sw.js')
def service_worker():
    response = send_from_directory('static', 'sw.js')
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
@rate_limited(8, 60)
def process_note():
    data = request.get_json(silent=True) or {}
    note_text = (data.get('text') or '').strip()
    mode = data.get('mode', 'full')
    if _is_banned(anon_id=(data.get('anon_id') or '').strip()[:64], email=session.get('user_email')):
        return jsonify({'status': 'error', 'message': 'ඔබේ account/device එකට මේ feature එක restrict කර ඇත.'}), 403
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

    if mode != 'smart':
        return jsonify({
            'status': 'success',
            'processed_text': note_text,
            'mermaid_code_si': '',
            'mermaid_code_en': '',
            'ai_processed': False,
            'warning': ''
        })

    _script_gen_start = time.time()
    try:
        podcast_script, mermaid_code_si, mermaid_code_en = call_gemini_structured(note_text, output_language)
        log_engine_event('script', 'gemini', None, time.time() - _script_gen_start, success=True)
    except GeminiGenerationError as gemini_error:
        print(f"Gemini Error: {gemini_error}")
        log_engine_event('script', 'gemini', None, time.time() - _script_gen_start, success=False, error_message=str(gemini_error))

        if not openai_client:
            return jsonify({
                'status': 'success',
                'processed_text': note_text,
                'mermaid_code_si': '',
                'mermaid_code_en': '',
                'ai_processed': False,
                'warning': _friendly_gemini_error_message(gemini_error)
            })

        # Gemini exhausted its own model/retry plan — fall back to
        # OpenAI so a Gemini outage doesn't take Smart Study down
        # entirely, rather than immediately giving up on AI processing.
        print("⚠️ Gemini failed — falling back to OpenAI for script generation...")
        _openai_script_start = time.time()
        try:
            podcast_script, mermaid_code_si, mermaid_code_en = call_openai_structured(note_text, output_language)
            log_engine_event('script', 'openai', None, time.time() - _openai_script_start, success=True)
        except OpenAIGenerationError as openai_error:
            print(f"OpenAI script fallback Error: {openai_error}")
            log_engine_event('script', 'openai', None, time.time() - _openai_script_start, success=False, error_message=str(openai_error))
            return jsonify({
                'status': 'success',
                'processed_text': note_text,
                'mermaid_code_si': '',
                'mermaid_code_en': '',
                'ai_processed': False,
                'warning': _friendly_gemini_error_message(gemini_error)
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
@rate_limited(15, 60)
def library_save():
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

    owner_google_id = session.get('user_id')

    source_image = data.get('source_image') or None
    MAX_SOURCE_IMAGE_CHARS = 700_000
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
        query = (request.args.get('q') or '').strip()
        like_term = f'%{query}%'

        if user_id:
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
        device_info = parse_device_info(request.headers.get('User-Agent', ''))
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
        return jsonify({'status': 'error'}), 200


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


def _is_banned(anon_id=None, email=None):
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
        return False


def _is_admin_logged_in():
    return session.get('is_admin') is True


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
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
    # FIX (Aug 18, 2026 — intermittent /admin/data 500 errors +
    # "Unclosed client session" warnings in Render logs): if any query
    # below raised an exception, the function exited immediately
    # WITHOUT ever reaching conn.close() further down — leaving the
    # underlying Turso network connection open/leaked. That leaked
    # connection would later get garbage-collected mid-flight during a
    # totally unrelated request, producing the "Unclosed client
    # session" warning seen in the logs right after a /process-note
    # call. Wrapping everything in try/finally guarantees conn.close()
    # runs — success or failure — exactly like the "always close your
    # umbrella" rule, whether it stopped raining or you gave up early.
    conn = get_db()
    try:
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

        month_start = datetime.now(timezone.utc).strftime('%Y-%m-01')
        gemini_calls_row = conn.execute(
            'SELECT COUNT(*) FROM gemini_calls WHERE created_at >= ?', (month_start,)
        ).fetchone()
        gemini_calls_this_month = gemini_calls_row[0] if gemini_calls_row else 0
    finally:
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


# NEW (Aug 20, 2026): "Engine Health" admin insights — built on top of
# the engine_events table logged from /tts and /process-note above.
# Three views: which engines/models are actually used and their cost,
# how often each one fails, and how processing time trends day to day.
@app.route('/admin/engine-breakdown')
def admin_engine_breakdown():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT
                event_type,
                engine,
                COALESCE(model_version, '—') AS model_version,
                COUNT(*) AS total_calls,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS fail_count,
                AVG(CASE WHEN success = 1 THEN duration_seconds ELSE NULL END) AS avg_duration_seconds
            FROM engine_events
            GROUP BY event_type, engine, model_version
            ORDER BY total_calls DESC
        """).fetchall()
        conn.close()

        breakdown = []
        for row in rows:
            total = row['total_calls'] or 0
            fails = row['fail_count'] or 0
            breakdown.append({
                'event_type': row['event_type'],
                'engine': row['engine'],
                'model_version': row['model_version'],
                'total_calls': total,
                'success_count': row['success_count'] or 0,
                'fail_count': fails,
                'fail_rate_pct': round((fails / total) * 100, 1) if total else 0,
                'avg_duration_seconds': round(row['avg_duration_seconds'], 1) if row['avg_duration_seconds'] is not None else None,
            })
        return jsonify({'status': 'success', 'breakdown': breakdown})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/processing-time-trend')
def admin_processing_time_trend():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT
                substr(created_at, 1, 10) AS day,
                event_type,
                AVG(CASE WHEN success = 1 THEN duration_seconds ELSE NULL END) AS avg_duration
            FROM engine_events
            WHERE created_at >= datetime('now', '-7 days')
            GROUP BY day, event_type
            ORDER BY day ASC
        """).fetchall()
        conn.close()

        days_map = {}
        for row in rows:
            day = row['day']
            days_map.setdefault(day, {'day': day, 'tts': None, 'script': None})
            avg_val = round(row['avg_duration'], 1) if row['avg_duration'] is not None else None
            if row['event_type'] == 'tts':
                days_map[day]['tts'] = avg_val
            elif row['event_type'] == 'script':
                days_map[day]['script'] = avg_val

        trend = sorted(days_map.values(), key=lambda d: d['day'])
        return jsonify({'status': 'success', 'trend': trend})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/admin/device-breakdown')
def admin_device_breakdown():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        conn = get_db()
        # device_info is stored as "Browser · OS · DeviceType" (see
        # parse_device_info) — split it into 3 separate breakdowns
        # rather than one giant list of exact combinations, since
        # "Chrome · Android · Mobile" vs "Chrome · Windows · Desktop"
        # buried in one flat list is much harder to scan at a glance.
        rows = conn.execute("""
            SELECT device_info, COUNT(*) AS cnt
            FROM (
                SELECT anon_id, device_info, MAX(created_at) AS latest
                FROM usage_events
                WHERE device_info IS NOT NULL
                GROUP BY anon_id
            )
            GROUP BY device_info
        """).fetchall()
        conn.close()

        browsers, oses, device_types = {}, {}, {}
        for row in rows:
            parts = [p.strip() for p in (row['device_info'] or '').split('·')]
            cnt = row['cnt']
            if len(parts) == 3:
                browser, os_name, dtype = parts
                browsers[browser] = browsers.get(browser, 0) + cnt
                oses[os_name] = oses.get(os_name, 0) + cnt
                device_types[dtype] = device_types.get(dtype, 0) + cnt

        def to_sorted_list(d):
            return sorted([{'label': k, 'count': v} for k, v in d.items()], key=lambda x: x['count'], reverse=True)

        return jsonify({
            'status': 'success',
            'browsers': to_sorted_list(browsers),
            'operating_systems': to_sorted_list(oses),
            'device_types': to_sorted_list(device_types),
        })
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
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        data = request.get_json(silent=True) or {}
        note_ids = data.get('note_ids') or []
        note_ids = [int(i) for i in note_ids if str(i).isdigit()][:200]
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
        global _banned_cache_last_refresh
        _banned_cache_last_refresh = 0
        return jsonify({'status': 'success', 'action': action})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/library/report/<int:note_id>', methods=['POST'])
@rate_limited(5, 300)
def report_note(note_id):
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
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        conn = get_db()

        signed_in_coins = conn.execute("SELECT COALESCE(SUM(coins), 0) AS total FROM users").fetchone()['total']
        guest_coins = conn.execute("SELECT COALESCE(SUM(coins), 0) AS total FROM user_state").fetchone()['total']

        image_stats = conn.execute("""
            SELECT COUNT(*) AS image_count, COALESCE(SUM(LENGTH(source_image_data)), 0) AS total_bytes
            FROM notes WHERE source_image_data IS NOT NULL
        """).fetchone()
        image_storage_mb = round(image_stats['total_bytes'] / (1024 * 1024), 2)
        FREE_TIER_STORAGE_GB = 5
        image_storage_pct_of_free_tier = round((image_stats['total_bytes'] / (FREE_TIER_STORAGE_GB * 1024 * 1024 * 1024)) * 100, 2)

        per_device = conn.execute("""
            SELECT anon_id,
                   (SELECT user_name FROM usage_events ue2 WHERE ue2.anon_id = ue1.anon_id AND ue2.user_name IS NOT NULL ORDER BY ue2.created_at DESC LIMIT 1) AS display_name,
                   SUM(CASE WHEN action = 'note_processed' THEN 1 ELSE 0 END) AS notes_processed
            FROM usage_events ue1
            GROUP BY anon_id
            HAVING notes_processed > 0
        """).fetchall()

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

        now = datetime.now(timezone.utc)
        wau_cutoff = (now - timedelta(days=7)).isoformat()
        mau_cutoff = (now - timedelta(days=30)).isoformat()
        wau = conn.execute("SELECT COUNT(DISTINCT anon_id) AS c FROM usage_events WHERE created_at >= ?", (wau_cutoff,)).fetchone()['c']
        mau = conn.execute("SELECT COUNT(DISTINCT anon_id) AS c FROM usage_events WHERE created_at >= ?", (mau_cutoff,)).fetchone()['c']

        conn.close()

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
        target_anon_id = (data.get('target_anon_id') or '').strip()[:64] or None
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


# ========================================
# DIGITAL TUITION SIR — student-facing course routes
# ========================================

@app.route('/courses/list', methods=['GET'])
def student_list_courses():
    """Public — browsable without logging in (matches the free note
    flow's own no-login-required browsing), only login-gated at
    Enroll. Only published courses; lesson_count only counts published
    lessons so a course with lessons still mid-authoring doesn't show
    an inflated/misleading count to students."""
    conn = get_db()
    rows = conn.execute("""
        SELECT c.id, c.grade, c.subject, c.title, c.exam_target, c.price_lkr, c.free_trial_days,
               COUNT(l.id) AS lesson_count
        FROM courses c
        LEFT JOIN course_lessons l ON l.course_id = c.id AND l.status = 'published'
        WHERE c.status = 'published'
        GROUP BY c.id
        ORDER BY c.grade, c.subject
    """).fetchall()
    conn.close()
    return jsonify({'status': 'success', 'courses': [dict(r) for r in rows]})


@app.route('/courses/<int:course_id>', methods=['GET'])
def student_course_detail(course_id):
    conn = get_db()
    course = conn.execute("SELECT * FROM courses WHERE id = ? AND status = 'published'", (course_id,)).fetchone()
    if not course:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Course එක හම්බුනේ නෑ.'}), 404
    lessons = conn.execute(
        "SELECT id, day_number, title FROM course_lessons WHERE course_id = ? AND status = 'published' ORDER BY day_number ASC",
        (course_id,)
    ).fetchall()

    enrolled = False
    user_id = session.get('user_id')
    if user_id:
        enrollment = conn.execute(
            "SELECT id FROM user_course_enrollments WHERE user_google_id = ? AND course_id = ?",
            (user_id, course_id)
        ).fetchone()
        enrolled = enrollment is not None
    conn.close()

    return jsonify({
        'status': 'success',
        'course': dict(course),
        'lessons': [dict(l) for l in lessons],
        'enrolled': enrolled,
    })


@app.route('/courses/<int:course_id>/enroll', methods=['POST'])
def student_enroll_course(course_id):
    if not session.get('user_id'):
        return jsonify({'status': 'error', 'message': 'Enroll වෙන්න Google account එකකින් login වෙන්න ඕන.', 'login_required': True}), 401

    conn = get_db()
    course = conn.execute("SELECT id FROM courses WHERE id = ? AND status = 'published'", (course_id,)).fetchone()
    if not course:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Course එක හම්බුනේ නෑ.'}), 404

    conn.execute(
        """INSERT INTO user_course_enrollments (user_google_id, course_id, enrolled_at)
           VALUES (?, ?, ?)
           ON CONFLICT(user_google_id, course_id) DO NOTHING""",
        (session['user_id'], course_id, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/my-learning', methods=['GET'])
def student_my_learning():
    """For each course the student is enrolled in: the course info plus
    their 'current' lesson — the first published lesson (by day_number)
    they haven't completed yet, or None if they've finished every
    published lesson so far (more may get published later)."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'success', 'courses': []})

    conn = get_db()
    enrollments = conn.execute(
        """SELECT c.* FROM user_course_enrollments e
           JOIN courses c ON c.id = e.course_id
           WHERE e.user_google_id = ?
           ORDER BY e.enrolled_at DESC""",
        (user_id,)
    ).fetchall()

    result = []
    for course in enrollments:
        course = dict(course)
        lessons = conn.execute(
            "SELECT id, day_number, title FROM course_lessons WHERE course_id = ? AND status = 'published' ORDER BY day_number ASC",
            (course['id'],)
        ).fetchall()
        completed_ids = {
            r['lesson_id'] for r in conn.execute(
                """SELECT lesson_id FROM user_lesson_progress
                   WHERE user_google_id = ? AND lesson_id IN (SELECT id FROM course_lessons WHERE course_id = ?)""",
                (user_id, course['id'])
            ).fetchall()
        }
        current_lesson = next((dict(l) for l in lessons if l['id'] not in completed_ids), None)
        result.append({
            'course': course,
            'total_lessons': len(lessons),
            'completed_lessons': len(completed_ids),
            'current_lesson': current_lesson,
        })
    conn.close()
    return jsonify({'status': 'success', 'courses': result})


@app.route('/lessons/<int:lesson_id>/content', methods=['GET'])
def student_lesson_content(lesson_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'error', 'message': 'Login වෙන්න ඕන.', 'login_required': True}), 401

    conn = get_db()
    lesson = conn.execute("SELECT * FROM course_lessons WHERE id = ? AND status = 'published'", (lesson_id,)).fetchone()
    if not lesson:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Lesson එක හම්බුනේ නෑ.'}), 404
    lesson = dict(lesson)

    enrollment = conn.execute(
        "SELECT id FROM user_course_enrollments WHERE user_google_id = ? AND course_id = ?",
        (user_id, lesson['course_id'])
    ).fetchone()
    if not enrollment:
        conn.close()
        return jsonify({'status': 'error', 'message': 'මේ course එකට enroll වෙලා නෑ.', 'enroll_required': True}), 403

    course = conn.execute("SELECT * FROM courses WHERE id = ?", (lesson['course_id'],)).fetchone()
    completed = conn.execute(
        "SELECT id FROM user_lesson_progress WHERE user_google_id = ? AND lesson_id = ?",
        (user_id, lesson_id)
    ).fetchone() is not None
    conn.close()

    is_manual = lesson['active_audio_source'] == 'manual'
    audio_url = lesson['manual_audio_url'] if is_manual else lesson['ai_audio_url']
    # A manual recording has no per-sentence timing data (nothing
    # generated it) — the player falls back to plain, unhighlighted
    # text for those, same as the quick-note flow does when timings
    # are missing.
    sentence_timings = None if is_manual else json.loads(lesson['ai_audio_timings'] or 'null')
    return jsonify({
        'status': 'success',
        'lesson': {
            'id': lesson['id'], 'day_number': lesson['day_number'], 'title': lesson['title'],
            'script_text': lesson['script_text'], 'mermaid_code_si': lesson['mermaid_code_si'],
            'mermaid_code_en': lesson['mermaid_code_en'], 'audio_url': audio_url,
            'sentence_timings': sentence_timings,
            'completed': completed,
        },
        'course': dict(course),
    })


@app.route('/lessons/<int:lesson_id>/complete', methods=['POST'])
def student_complete_lesson(lesson_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'error', 'message': 'Login වෙන්න ඕන.'}), 401

    conn = get_db()
    lesson = conn.execute("SELECT * FROM course_lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Lesson එක හම්බුනේ නෑ.'}), 404

    conn.execute(
        """INSERT INTO user_lesson_progress (user_google_id, lesson_id, completed_at)
           VALUES (?, ?, ?)
           ON CONFLICT(user_google_id, lesson_id) DO NOTHING""",
        (user_id, lesson_id, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()

    next_lesson = conn.execute(
        """SELECT id, day_number, title FROM course_lessons
           WHERE course_id = ? AND status = 'published' AND day_number > ?
           ORDER BY day_number ASC LIMIT 1""",
        (lesson['course_id'], lesson['day_number'])
    ).fetchone()
    conn.close()
    return jsonify({'status': 'success', 'next_lesson': dict(next_lesson) if next_lesson else None})


# ========================================
# DIGITAL TUITION SIR — admin course management
# ========================================

def _lesson_row_to_dict(row):
    d = dict(row)
    return d


@app.route('/admin/courses', methods=['POST'])
def admin_create_course():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    grade = (data.get('grade') or '').strip()
    subject = (data.get('subject') or '').strip()
    title = (data.get('title') or '').strip()
    if not grade or not subject or not title:
        return jsonify({'status': 'error', 'message': 'Grade, subject, title තුනම ඕන.'}), 400

    exam_target = (data.get('exam_target') or '').strip() or None
    price_lkr = data.get('price_lkr')
    try:
        price_lkr = int(price_lkr) if price_lkr not in (None, '') else None
    except (TypeError, ValueError):
        price_lkr = None
    try:
        free_trial_days = int(data.get('free_trial_days') or 0)
    except (TypeError, ValueError):
        free_trial_days = 0

    conn = get_db()
    conn.execute(
        """INSERT INTO courses (grade, subject, title, exam_target, price_lkr, free_trial_days, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)""",
        (grade, subject, title, exam_target, price_lkr, free_trial_days, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    new_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.close()
    return jsonify({'status': 'success', 'id': new_id})


@app.route('/admin/courses-list', methods=['GET'])
def admin_list_courses():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    conn = get_db()
    rows = conn.execute("""
        SELECT c.id, c.grade, c.subject, c.title, c.exam_target, c.price_lkr, c.free_trial_days,
               c.status, c.created_at,
               COUNT(l.id) AS lesson_count,
               COALESCE(SUM(CASE WHEN l.status = 'published' THEN 1 ELSE 0 END), 0) AS published_count
        FROM courses c
        LEFT JOIN course_lessons l ON l.course_id = c.id
        GROUP BY c.id
        ORDER BY c.created_at DESC
    """).fetchall()
    conn.close()
    return jsonify({'status': 'success', 'courses': [dict(r) for r in rows]})


@app.route('/admin/courses/<int:course_id>/delete', methods=['POST'])
def admin_delete_course(course_id):
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    conn = get_db()
    conn.execute("DELETE FROM course_lessons WHERE course_id = ?", (course_id,))
    conn.execute("DELETE FROM courses WHERE id = ?", (course_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


MAX_OCR_BATCH_PAGES = 15  # pages OCR'd per Vision API round before the job row's processed_pages is updated — kept modest given Render's 512MB memory ceiling (see the 1.5x-zoom fix above)
SPLIT_CHUNK_WORDS = 2400  # words per Gemini split call — see _text_to_chunks


@app.route('/admin/courses/<int:course_id>/pdf-job/start', methods=['POST'])
def admin_pdf_job_start(course_id):
    # Course PDFs are curated admin uploads, not a public endpoint —
    # override the app-wide 5MB cap for this request only. MUST happen
    # before any request.files/request.form access below.
    request.max_content_length = 60 * 1024 * 1024

    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    if 'pdf' not in request.files:
        return jsonify({'status': 'error', 'message': 'PDF file එකක් attach කරලා නෑ.'}), 400
    if not CLOUD_OCR_AVAILABLE:
        return jsonify({'status': 'error', 'message': 'Cloud Vision API configure වී නෑ.'}), 500

    try:
        min_words = int(request.form.get('min_words') or 100)
        max_words = int(request.form.get('max_words') or 220)
    except (TypeError, ValueError):
        min_words, max_words = 100, 220

    pdf_bytes = request.files['pdf'].read()

    MAX_SPLIT_PAGES = 200
    try:
        page_count_probe = fitz.open(stream=pdf_bytes, filetype='pdf')
        total_pdf_pages = len(page_count_probe)
        page_count_probe.close()
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'PDF එක open කරගැනීම අසාර්ථක විය: {e}'}), 400

    if total_pdf_pages > MAX_SPLIT_PAGES:
        return jsonify({
            'status': 'error',
            'message': (
                f'මේ PDF එකේ pages {total_pdf_pages}ක් තියෙනවා — උපරිම {MAX_SPLIT_PAGES}ක් '
                f'විතරයි support කරන්නේ. කරුණාකර පොත කොටස් 2ක/3කට split කරලා, එකින් එක upload '
                f'කරන්න.'
            )
        }), 400

    conn = get_db()
    conn.execute(
        """INSERT INTO pdf_split_jobs
           (course_id, status, phase, total_pages, processed_pages, full_text, min_words, max_words, created_at)
           VALUES (?, 'processing', 'ocr', ?, 0, '', ?, ?, ?)""",
        (course_id, total_pdf_pages, min_words, max_words, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    job_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.close()

    return jsonify({'status': 'success', 'job_id': job_id, 'total_pages': total_pdf_pages})


@app.route('/admin/courses/<int:course_id>/pdf-job/ocr-step', methods=['POST'])
def admin_pdf_job_ocr_step(course_id):
    # Same ordering requirement as pdf-job/start — before request.files access.
    request.max_content_length = 60 * 1024 * 1024

    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    if 'pdf' not in request.files or 'job_id' not in request.form:
        return jsonify({'status': 'error', 'message': 'PDF file එක හෝ job_id එක නෑ.'}), 400

    job_id = int(request.form['job_id'])
    conn = get_db()
    job = conn.execute("SELECT * FROM pdf_split_jobs WHERE id = ? AND course_id = ?", (job_id, course_id)).fetchone()
    if not job:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Job එක හම්බුනේ නෑ.'}), 404
    job = dict(job)

    if job['phase'] != 'ocr':
        conn.close()
        return jsonify({'status': 'success', 'phase': job['phase'], 'job_id': job_id})

    pdf_bytes = request.files['pdf'].read()

    try:
        batch_text, total_pages, end_page = _ocr_pdf_pages_to_text(
            pdf_bytes, start_page=job['processed_pages'], max_pages=MAX_OCR_BATCH_PAGES
        )
    except Exception as e:
        conn.execute("UPDATE pdf_split_jobs SET status = 'error', error_message = ? WHERE id = ?", (str(e), job_id))
        conn.commit()
        conn.close()
        return jsonify({'status': 'error', 'message': f'PDF එකෙන් text extract කරගැනීම අසාර්ථක විය: {e}'}), 500

    full_text = (job['full_text'] or '') + ('\n\n' if job['full_text'] else '') + batch_text

    if end_page >= total_pages:
        # OCR done — move to the split phase.
        if len(full_text.strip()) < 20:
            conn.execute("UPDATE pdf_split_jobs SET status = 'error', error_message = ? WHERE id = ?",
                         ('මේ PDF එකෙන් text extract කරගන්න බැරි උනා.', job_id))
            conn.commit()
            conn.close()
            return jsonify({'status': 'error', 'message': 'මේ PDF එකෙන් text extract කරගන්න බැරි උනා.'}), 400

        chunks = _text_to_chunks(full_text, words_per_chunk=SPLIT_CHUNK_WORDS)
        conn.execute(
            """UPDATE pdf_split_jobs SET processed_pages = ?, full_text = ?, phase = 'split',
               total_chunks = ?, processed_chunks = 0, result_json = '[]' WHERE id = ?""",
            (end_page, full_text, len(chunks), job_id)
        )
        conn.commit()
        conn.close()
        return jsonify({
            'status': 'success', 'phase': 'split', 'job_id': job_id,
            'processed_pages': end_page, 'total_pages': total_pages,
            'total_chunks': len(chunks), 'processed_chunks': 0,
        })

    conn.execute("UPDATE pdf_split_jobs SET processed_pages = ?, full_text = ? WHERE id = ?", (end_page, full_text, job_id))
    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success', 'phase': 'ocr', 'job_id': job_id,
        'processed_pages': end_page, 'total_pages': total_pages,
    })


@app.route('/admin/courses/<int:course_id>/pdf-job/split-step', methods=['POST'])
def admin_pdf_job_split_step(course_id):
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    job_id = data.get('job_id')
    if not job_id:
        return jsonify({'status': 'error', 'message': 'job_id එකක් නෑ.'}), 400

    conn = get_db()
    job = conn.execute("SELECT * FROM pdf_split_jobs WHERE id = ? AND course_id = ?", (job_id, course_id)).fetchone()
    if not job:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Job එක හම්බුනේ නෑ.'}), 404
    job = dict(job)

    if job['phase'] != 'split':
        conn.close()
        return jsonify({'status': 'error', 'message': 'OCR phase එක තවම ඉවර වී නෑ.'}), 400

    chunks = _text_to_chunks(job['full_text'] or '', words_per_chunk=SPLIT_CHUNK_WORDS)
    idx = job['processed_chunks']

    if idx >= len(chunks):
        conn.close()
        return jsonify({'status': 'error', 'message': 'Split step index එක invalid.'}), 400

    split_instruction = build_split_system_instruction(job['min_words'] or 100, job['max_words'] or 220)
    try:
        chunk_lessons = _split_one_gemini_chunk(chunks[idx], split_instruction)
    except GeminiGenerationError as e:
        conn.execute("UPDATE pdf_split_jobs SET status = 'error', error_message = ? WHERE id = ?", (str(e), job_id))
        conn.commit()
        conn.close()
        return jsonify({'status': 'error', 'message': f'Lessons වලට split කරගැනීම අසාර්ථක විය: {e}'}), 500

    accumulated = json.loads(job['result_json'] or '[]') + chunk_lessons
    new_processed_chunks = idx + 1

    if new_processed_chunks >= len(chunks):
        for i, lesson in enumerate(accumulated, 1):
            lesson['day'] = i
        if not accumulated:
            conn.execute("UPDATE pdf_split_jobs SET status = 'error', error_message = ? WHERE id = ?",
                         ('PDF split produced no lessons.', job_id))
            conn.commit()
            conn.close()
            return jsonify({'status': 'error', 'message': 'PDF split එකෙන් lessons කිසිවක් ලැබුනේ නෑ.'}), 500

        conn.execute(
            "UPDATE pdf_split_jobs SET status = 'done', processed_chunks = ?, result_json = ? WHERE id = ?",
            (new_processed_chunks, json.dumps(accumulated, ensure_ascii=False), job_id)
        )
        conn.commit()
        conn.close()
        return jsonify({'status': 'done', 'lessons': accumulated})

    conn.execute(
        "UPDATE pdf_split_jobs SET processed_chunks = ?, result_json = ? WHERE id = ?",
        (new_processed_chunks, json.dumps(accumulated, ensure_ascii=False), job_id)
    )
    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success', 'phase': 'split',
        'processed_chunks': new_processed_chunks, 'total_chunks': len(chunks),
    })


@app.route('/admin/courses/<int:course_id>/lessons/save-batch', methods=['POST'])
def admin_save_course_lessons(course_id):
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    lessons = data.get('lessons') or []
    if not lessons:
        return jsonify({'status': 'error', 'message': 'Save කරන්න lessons නෑ.'}), 400

    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    for lesson in lessons:
        title = (lesson.get('title') or '').strip()[:200] or 'Untitled Lesson'
        source_text = (lesson.get('source_text') or '').strip()
        day_number = lesson.get('day')
        if not source_text or not day_number:
            continue
        conn.execute("""
            INSERT INTO course_lessons (course_id, day_number, title, source_text, status, created_at)
            VALUES (?, ?, ?, ?, 'draft', ?)
            ON CONFLICT(course_id, day_number) DO UPDATE SET
                title = excluded.title,
                source_text = excluded.source_text
        """, (course_id, day_number, title, source_text, now))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/admin/courses/<int:course_id>/lessons-list', methods=['GET'])
def admin_list_course_lessons(course_id):
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM course_lessons WHERE course_id = ? ORDER BY day_number ASC",
        (course_id,)
    ).fetchall()
    conn.close()
    return jsonify({'status': 'success', 'lessons': [_lesson_row_to_dict(r) for r in rows]})


@app.route('/admin/lessons/<int:lesson_id>/generate-script', methods=['POST'])
def admin_generate_lesson_script(lesson_id):
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    conn = get_db()
    lesson = conn.execute("SELECT * FROM course_lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Lesson එක හම්බුනේ නෑ.'}), 404

    try:
        script, mermaid_si, mermaid_en = call_teacher_script(lesson['source_text'])
    except GeminiGenerationError as e:
        conn.close()
        return jsonify({'status': 'error', 'message': f'Script generate කිරීම අසාර්ථක විය: {e}'}), 500

    conn.execute(
        "UPDATE course_lessons SET script_text = ?, mermaid_code_si = ?, mermaid_code_en = ? WHERE id = ?",
        (script, mermaid_si, mermaid_en, lesson_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'script_text': script, 'mermaid_code_si': mermaid_si, 'mermaid_code_en': mermaid_en})


@app.route('/admin/lessons/<int:lesson_id>/generate-audio', methods=['POST'])
def admin_generate_lesson_audio(lesson_id):
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    conn = get_db()
    lesson = conn.execute("SELECT * FROM course_lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Lesson එක හම්බුනේ නෑ.'}), 404
    if not lesson['script_text']:
        conn.close()
        return jsonify({'status': 'error', 'message': 'මුලින්ම script එක generate කරන්න ඕන.'}), 400

    try:
        audio_segment, engine_used, sentence_timings = generate_lesson_audio(lesson['script_text'])
    except Exception as e:
        conn.close()
        return jsonify({'status': 'error', 'message': f'Audio generate කිරීම අසාර්ථක විය: {e}'}), 500

    buf = io.BytesIO()
    audio_segment.export(buf, format='mp3')
    try:
        audio_url = upload_persistent_audio(buf.getvalue(), f'lesson_{lesson_id}_ai')
    except Exception as e:
        conn.close()
        return jsonify({'status': 'error', 'message': f'Audio upload අසාර්ථක විය: {e}'}), 500

    conn.execute(
        "UPDATE course_lessons SET ai_audio_url = ?, ai_audio_engine = ?, ai_audio_timings = ? WHERE id = ?",
        (audio_url, engine_used, json.dumps(sentence_timings, ensure_ascii=False), lesson_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'ai_audio_url': audio_url, 'ai_audio_engine': engine_used})


@app.route('/admin/lessons/<int:lesson_id>/upload-audio', methods=['POST'])
def admin_upload_lesson_audio(lesson_id):
    # Same ordering requirement as admin_pdf_job_start above — must
    # be set before any request.files/request.form access.
    request.max_content_length = 20 * 1024 * 1024

    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    if 'audio' not in request.files:
        return jsonify({'status': 'error', 'message': 'Audio file එකක් attach කරලා නෑ.'}), 400

    file = request.files['audio']
    ext = os.path.splitext(file.filename or '')[1].lower()
    if ext not in ('.mp3', '.wav', '.m4a', '.ogg'):
        return jsonify({'status': 'error', 'message': 'mp3/wav/m4a/ogg file එකක් upload කරන්න.'}), 400

    try:
        audio_url = upload_persistent_audio(file.read(), f'lesson_{lesson_id}_manual')
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Upload අසාර්ථක විය — file එක corrupt වෙන්න පුළුවන්: {e}'}), 500

    conn = get_db()
    conn.execute(
        "UPDATE course_lessons SET manual_audio_url = ?, active_audio_source = 'manual' WHERE id = ?",
        (audio_url, lesson_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'manual_audio_url': audio_url})


@app.route('/admin/lessons/<int:lesson_id>/set-audio-source', methods=['POST'])
def admin_set_lesson_audio_source(lesson_id):
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    source = data.get('source')
    if source not in ('ai', 'manual'):
        return jsonify({'status': 'error', 'message': "source එක 'ai' හෝ 'manual' විය යුතුය."}), 400
    conn = get_db()
    conn.execute("UPDATE course_lessons SET active_audio_source = ? WHERE id = ?", (source, lesson_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/admin/lessons/<int:lesson_id>/publish', methods=['POST'])
def admin_publish_lesson(lesson_id):
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    new_status = 'published' if data.get('publish') else 'draft'
    conn = get_db()
    conn.execute("UPDATE course_lessons SET status = ? WHERE id = ?", (new_status, lesson_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'lesson_status': new_status})


@app.route('/admin/lessons/<int:lesson_id>/delete', methods=['POST'])
def admin_delete_lesson(lesson_id):
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    conn = get_db()
    conn.execute("DELETE FROM course_lessons WHERE id = ?", (lesson_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


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
    # FIX (Aug 20, 2026 — pre-launch security audit): default was
    # 'true'. This block only runs if someone executes `python app.py`
    # directly — gunicorn (Render's actual Start Command) imports this
    # file as a WSGI module and never reaches this line, so this was
    # NOT an active risk on the live site. Still, defaulting debug mode
    # ON is genuinely dangerous if this is ever run directly: Flask's
    # debug mode exposes an interactive in-browser Python console on
    # any unhandled exception, which is remote code execution if
    # anyone finds it. Defaulting to 'false' is the safe choice —
    # someone can still opt in explicitly for local development by
    # setting FLASK_DEBUG=true in their own .env.
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug_mode, host='0.0.0.0', port=port)