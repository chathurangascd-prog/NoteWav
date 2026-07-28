import os
import sys
import io
import re
import json
import time
import uuid
import glob
import sqlite3
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
from google import genai
from google.genai import types
from gtts import gTTS
from pydub import AudioSegment  # pip install pydub --break-system-packages
                                 # also requires ffmpeg installed on the system
from concurrent.futures import ThreadPoolExecutor

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

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB

# ========================================
# NOTES LIBRARY (SQLite — save/organize notes by subject)
# ========================================
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notewav.db')


def get_db():
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


def _tts_sentence_to_segment(args):
    sentence, lang = args
    buf = io.BytesIO()
    gTTS(text=sentence, lang=lang, slow=False, tld='com').write_to_fp(buf)
    buf.seek(0)
    segment = AudioSegment.from_file(buf, format='mp3')
    return segment.set_frame_rate(GTTS_FRAME_RATE).set_channels(1)


def synthesize_gtts_natural(text, lang='si', pause_ms=350, paragraph_pause_ms=600, max_workers=5):
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()] or [text]

    flat_sentences = []
    for p_index, paragraph in enumerate(paragraphs):
        sentences = split_into_sentences(paragraph) or [paragraph]
        for s_index, sentence in enumerate(sentences):
            is_last_in_paragraph = (s_index == len(sentences) - 1)
            flat_sentences.append((sentence, is_last_in_paragraph))
    if not flat_sentences:
        flat_sentences = [(text, True)]

    tasks = [(s, lang) for (s, _) in flat_sentences]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        segments = list(executor.map(_tts_sentence_to_segment, tasks))

    sentence_silence = AudioSegment.silent(duration=pause_ms, frame_rate=GTTS_FRAME_RATE)
    paragraph_silence = AudioSegment.silent(duration=paragraph_pause_ms, frame_rate=GTTS_FRAME_RATE)
    combined = AudioSegment.silent(duration=0, frame_rate=GTTS_FRAME_RATE)

    total = len(flat_sentences)
    for i, ((_, is_last_in_paragraph), segment) in enumerate(zip(flat_sentences, segments)):
        combined += segment
        if i == total - 1:
            break
        combined += paragraph_silence if is_last_in_paragraph else sentence_silence

    return combined


class GeminiGenerationError(Exception):
    pass


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
# FIX (chemical formulas like H₂O looked cramped/cut off in mind map
# nodes): subscript/superscript glyphs (₂, ², etc.) aren't sized like
# regular text in most fonts, so Mermaid's node-box height estimate
# (based on normal character metrics) doesn't leave enough room for
# them, making the label look visually clipped. Converting to plain
# digits (H2O instead of H₂O) sidesteps the font-metric mismatch
# entirely — this is a safety net in case Gemini uses them despite
# being instructed not to.
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
    """FIX (chemical formulas showed up as literal "$H_2O$" in the
    readable/narrated script): despite being instructed not to,
    Gemini sometimes writes chemical formulas or math using LaTeX-style
    notation ($...$ delimiters, _ for subscript, ^ for superscript,
    sometimes with {braces} for grouping too, e.g. "$H_{2}O$"). That's
    meaningless as plain text — it reads oddly out loud via TTS and
    looks broken on screen. This is a safety net: strip ALL LaTeX
    syntax characters (_, ^, {, }, \\) from inside $...$ math sections,
    then remove the $ delimiters themselves — leaving just the plain
    alphanumeric content. Also catches stray _ / ^ markers that show up
    OUTSIDE any $...$ wrapper, in case Gemini drops the delimiters but
    keeps the subscript syntax.
    """
    if not text or not isinstance(text, str):
        return text
    text = text.translate(_SUBSCRIPT_SUPERSCRIPT_MAP)

    def strip_latex_markup(match):
        inner = match.group(1)
        return re.sub(r'[_^{}\\]', '', inner)

    text = re.sub(r'\$([^$]+)\$', strip_latex_markup, text)
    # Safety net for stray _ / ^ markers outside any $...$ wrapper.
    text = re.sub(r'(?<=[A-Za-z0-9])_(?=[A-Za-z0-9])', '', text)
    text = re.sub(r'(?<=[A-Za-z0-9])\^(?=[A-Za-z0-9])', '', text)
    return text


def _is_transient_gemini_error(exc):
    """Distinguishes a transient server-side hiccup (worth a quick
    retry) from a permanent problem (bad API key, safety block,
    malformed request) where retrying would just waste time and hit
    the same failure again."""
    msg = str(exc).upper()
    transient_markers = ['500', '502', '503', '504', 'INTERNAL', 'UNAVAILABLE', 'TIMEOUT', 'DEADLINE_EXCEEDED']
    return any(marker in msg for marker in transient_markers)


def _is_rate_limit_error(exc):
    """A 429/RESOURCE_EXHAUSTED means the API key's quota (often the
    free tier's daily/per-minute request cap) is used up. This is NOT
    the same kind of "transient" as a 500 — Google's own error message
    suggests waiting up to ~35 seconds, which is too long to block a
    web request for (risks the server's own request timeout killing
    it first). So this gets its own short-retry-then-clear-message
    path instead of the generic transient retry loop.
    """
    msg = str(exc).upper()
    return '429' in msg or 'RESOURCE_EXHAUSTED' in msg or 'QUOTA' in msg


def call_gemini_structured(note_text, max_retries=3):
    if not client:
        raise GeminiGenerationError("Gemini API is not configured (missing GEMINI_API_KEY).")

    prompt = f"""පහත පාඩම් සටහන සකසන්න:

{note_text}
"""

    # FIX (occasional "Gemini request failed: 500 INTERNAL" shown to
    # students): that error is a transient hiccup on Google's servers,
    # not a bug here — it usually succeeds on a quick retry. Rather
    # than making the student notice this and manually click "try
    # again", retry automatically a few times with a short backoff
    # before giving up and surfacing an error. Non-transient failures
    # (bad API key, safety blocks, malformed requests) are NOT
    # retried — retrying those would just waste time reproducing the
    # same permanent failure.
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model='gemini-flash-latest',
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
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
                # One short retry only (a few seconds) in case it was a
                # brief per-minute burst — but don't try to wait out a
                # full 30+ second quota window inside a live request.
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
        # Loop exhausted without a successful break (shouldn't normally
        # reach here since the last iteration always raises above, but
        # kept as a safety net).
        raise GeminiGenerationError(f"Gemini request failed after {max_retries} attempts: {last_error}")

    if not getattr(response, 'candidates', None):
        block_reason = getattr(getattr(response, 'prompt_feedback', None), 'block_reason', 'unknown')
        raise GeminiGenerationError(f"Content was blocked by Gemini's safety filters ({block_reason}).")

    candidate = response.candidates[0]
    finish_reason = str(getattr(candidate, 'finish_reason', '') or '')
    if 'SAFETY' in finish_reason.upper():
        raise GeminiGenerationError("Content was blocked by Gemini's safety filters.")
    if 'MAX_TOKENS' in finish_reason.upper():
        # FIX (helps diagnose "response could not be parsed as JSON"
        # failures): if Gemini hits the output token cap mid-generation,
        # the JSON gets cut off mid-string and fails to parse — but the
        # generic parse-failure message didn't say WHY. This gives a
        # much clearer signal when it happens again.
        raise GeminiGenerationError(
            "Gemini's response was cut off (hit the output length limit) — try shortening the note a bit."
        )

    raw_text = response.text
    if not raw_text:
        raise GeminiGenerationError("Gemini returned an empty response.")

    data = _parse_json_loose(raw_text)

    podcast_script = _clean_podcast_script((data.get('podcast_script') or '').strip())
    if '$' in podcast_script or '_' in podcast_script:
        # Diagnostic only — helps confirm in Render's logs whether the
        # cleanup actually ran and what (if anything) slipped through,
        # without blocking the response.
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
        audio_segment = synthesize_gtts_natural(formatted_text, lang=gtts_lang)
        unique_name = f"output_{uuid.uuid4().hex}.mp3"
        filename = os.path.join('static', unique_name)
        audio_segment.export(filename, format='mp3')
        print("✅ TTS Success!")
        return jsonify({'status': 'success', 'audio_url': '/' + filename.replace('\\', '/')})
    except Exception as e:
        print(f"❌ gTTS Error: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': 'gTTS හරහා audio එක generate කරගැනීම අසාර්ථක විය. පසුව උත්සාහ කරන්න.'
        }), 500


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/process-note', methods=['POST'])
def process_note():
    data = request.get_json(silent=True) or {}
    note_text = (data.get('text') or '').strip()
    mode = data.get('mode', 'full')

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
        podcast_script, mermaid_code_si, mermaid_code_en = call_gemini_structured(note_text)
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
    data = request.get_json(silent=True) or {}
    subject = (data.get('subject') or 'General').strip()[:80] or 'General'
    note_text = (data.get('note_text') or '').strip()
    processed_text = data.get('processed_text') or ''
    mermaid_code_si = data.get('mermaid_code_si') or ''
    mermaid_code_en = data.get('mermaid_code_en') or ''
    mode = data.get('mode') or 'full'

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

    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO notes
               (subject, title, note_text, processed_text, mermaid_code_si, mermaid_code_en, mode, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (subject, title, note_text, processed_text, mermaid_code_si, mermaid_code_en, mode,
             datetime.now(timezone.utc).isoformat())
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
    """Returns every saved note with FULL data (not just the summary
    fields library_list gives) — used by the frontend's backup/export
    feature, since the notes database on Render's free tier isn't
    guaranteed to survive a redeploy. Downloading this as a JSON file
    lets a student restore their notes later via /library/save."""
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    debug_mode = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(debug=debug_mode, host='0.0.0.0', port=port)