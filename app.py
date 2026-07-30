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
                                 # also requires ffmpeg installed on the system
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

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

# Simple password gate for the /admin usage dashboard. Set
# ADMIN_PASSWORD in Render's environment variables — do NOT hardcode
# a real password here in source control.
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB

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


def _is_rate_limit_error(exc):
    msg = str(exc).upper()
    return '429' in msg or 'RESOURCE_EXHAUSTED' in msg or 'QUOTA' in msg


def call_gemini_structured(note_text, output_language='si', max_retries=3):
    if not client:
        raise GeminiGenerationError("Gemini API is not configured (missing GEMINI_API_KEY).")

    prompt = f"""පහත පාඩම් සටහන සකසන්න:

{note_text}
"""

    system_instruction = build_system_instruction(output_language)

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model='gemini-flash-latest',
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.3,
                    max_output_tokens=8000,
                    response_mime_type='application/json',
                    safety_settings=SAFETY_SETTINGS,
                )
            )
            break  # success — exit the retry loop
        except Exception as e:
            last_error = e

            if _is_rate_limit_error(e):
                if attempt < 2:
                    print(f"⚠️ Gemini rate-limited (attempt {attempt}), short retry in 4s: {e}")
                    time.sleep(4)
                    continue
                raise GeminiGenerationError(
                    "Gemini API එකේ දෛනික/විනාඩි quota එක ඉවර වී ඇත (free tier limit). "
                    "පැය කිහිපයකින් හෝ මිනිත්තු කිහිපයකින් නැවත උත්සාහ කරන්න, නැතහොත් "
                    "Gemini API එකේ paid billing plan එකකට upgrade කරන්න."
                )

            if attempt < max_retries and _is_transient_gemini_error(e):
                wait_seconds = attempt  # 1s, then 2s, then 3s
                print(f"⚠️ Gemini transient error (attempt {attempt}/{max_retries}), retrying in {wait_seconds}s: {e}")
                time.sleep(wait_seconds)
                continue
            raise GeminiGenerationError(f"Gemini request failed: {e}")
    else:
        raise GeminiGenerationError(f"Gemini request failed after {max_retries} attempts: {last_error}")

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

    return podcast_script, mermaid_code_si, mermaid_code_en


@app.route('/ocr', methods=['POST'])
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
def text_to_speech():
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()

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


@app.route('/process-note', methods=['POST'])
def process_note():
    data = request.get_json(silent=True) or {}
    note_text = (data.get('text') or '').strip()
    mode = data.get('mode', 'full')
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
            'warning': 'Gemini API configure වී නොමැති බැවින් Smart Study සහ Mind Map ලබාගත නොහැක.'
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
            'warning': f'AI processing එක අසාර්ථක විය ({e}). ඔබේ මුල් text එකම පෙන්වයි.'
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

    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO notes
               (subject, title, note_text, processed_text, mermaid_code_si, mermaid_code_en, mode, created_at, anon_id, user_name, owner_google_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subject, title, note_text, processed_text, mermaid_code_si, mermaid_code_en, mode,
             datetime.now(timezone.utc).isoformat(), anon_id, user_name, owner_google_id)
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
        if user_id:
            # Signed in: PRIVATE list — only this account's own notes,
            # so it's the same list on every device they log into.
            rows = conn.execute(
                'SELECT id, subject, title, mode, created_at FROM notes WHERE owner_google_id = ? ORDER BY created_at DESC',
                (user_id,)
            ).fetchall()
        else:
            # Guest: UNCHANGED — the original shared/global listing,
            # exactly as it always worked before Google Sign-In existed.
            rows = conn.execute(
                'SELECT id, subject, title, mode, created_at FROM notes ORDER BY created_at DESC'
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


def _is_admin_logged_in():
    return session.get('is_admin') is True


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == ADMIN_PASSWORD:
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
            (SELECT coins FROM user_state us WHERE us.anon_id = ue1.anon_id) AS coins
        FROM usage_events ue1
        GROUP BY anon_id
        ORDER BY last_seen DESC
    """).fetchall()

    total_notes_in_library = conn.execute('SELECT COUNT(*) FROM notes').fetchone()[0]
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
        return jsonify({'status': 'success', 'events': events})
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


@app.route('/admin/announcements-list')
def admin_announcements_list():
    if not _is_admin_logged_in():
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, message, created_at, target_anon_id FROM announcements ORDER BY id DESC LIMIT 30"
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
        if not message:
            return jsonify({'status': 'error', 'message': 'Message එකක් ලියන්න.'}), 400
        conn = get_db()
        conn.execute(
            "INSERT INTO announcements (message, created_at, target_anon_id) VALUES (?, ?, ?)",
            (message, datetime.now(timezone.utc).isoformat(), target_anon_id)
        )
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        print(f"❌ Admin announce error: {e}")
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