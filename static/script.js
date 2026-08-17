// FIX (textarea grew indefinitely tall on mobile as text was typed —
// pushing everything below it further and further down the page,
// making mobile scrolling awkward): cap the auto-grow height at a
// reasonable max, and let the textarea scroll INTERNALLY (its own
// scrollbar) once content exceeds that, instead of the whole page
// growing without limit.
function autoResizeTextarea(el, maxHeightPx) {
    maxHeightPx = maxHeightPx || 320;
    el.style.height = 'auto';
    const neededHeight = el.scrollHeight;
    if (neededHeight > maxHeightPx) {
        el.style.height = maxHeightPx + 'px';
        el.style.overflowY = 'auto';
    } else {
        el.style.height = neededHeight + 'px';
        el.style.overflowY = 'hidden';
    }
}

// ========================================
// LIGHTWEIGHT ANONYMOUS USAGE TRACKING (for the admin dashboard)
// ========================================
function getOrCreateAnonId() {
    const KEY = 'notewav_anon_id';
    try {
        let id = localStorage.getItem(KEY);
        if (!id) {
            id = (crypto.randomUUID ? crypto.randomUUID() : 'anon-' + Date.now() + '-' + Math.random().toString(36).slice(2));
            localStorage.setItem(KEY, id);
        }
        return id;
    } catch (e) {
        return 'anon-fallback-' + Date.now();
    }
}

function trackUsageEvent(action) {
    try {
        let userName = '';
        const profile = JSON.parse(localStorage.getItem('notewav_profile') || '{}');
        if (profile.name) userName = profile.name.trim();

        let coins = null;
        try {
            coins = (typeof getCoinsBalance === 'function') ? getCoinsBalance() : null;
        } catch (e) {
            coins = null;
        }

        fetch('/track', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                anon_id: getOrCreateAnonId(),
                user_name: userName,
                action: action,
                coins: coins,
            }),
        }).catch(() => { /* tracking is best-effort only, never disrupt the actual feature */ });
    } catch (e) {
        // ignore — tracking must never break the app
    }
}

// ========================================
// GOOGLE SIGN-IN (account sync for coins/streak/library)
// ========================================
// Set once auth status is known — used by addCoins/spendCoins and the
// streak tracker to decide whether to also push updates to the server
// (signed-in users) or stay purely device-local (guests, unchanged
// from before Google Sign-In existed).
let notewavIsLoggedIn = false;

async function checkGoogleAuthStatus() {
    try {
        const res = await fetch('/auth/me');
        const data = await res.json();
        notewavIsLoggedIn = !!data.logged_in;

        const signinBtn = document.getElementById('google-signin-btn');
        const signedInInfo = document.getElementById('google-signed-in-info');
        const signedInName = document.getElementById('google-signed-in-name');
        const signedInEmail = document.getElementById('google-signed-in-email');

        if (data.logged_in) {
            if (signinBtn) signinBtn.classList.add('hidden');
            if (signedInInfo) signedInInfo.classList.remove('hidden');
            if (signedInName) signedInName.textContent = data.name || 'Google User';
            if (signedInEmail) signedInEmail.textContent = data.email || '';

            // Server is now the source of truth for coins/streak — pull
            // the account's saved values down and overwrite whatever
            // was in localStorage (so every device shows the same
            // numbers once signed in).
            if (typeof data.coins === 'number') {
                try { localStorage.setItem('notewav_coins', String(data.coins)); } catch (e) { /* ignore */ }
                if (typeof updateCoinsDisplay === 'function') updateCoinsDisplay();
            }
            if (typeof data.streak === 'number') {
                try {
                    localStorage.setItem('notewav_streak_data', JSON.stringify({
                        lastDate: data.last_streak_date || todayLocalDateString(),
                        streak: data.streak,
                    }));
                } catch (e) { /* ignore */ }
                const streakEl = document.getElementById('streak-count-drawer');
                if (streakEl) streakEl.textContent = `${data.streak} days`;
            }
            // Also reflect the Google name/picture in the local profile
            // fields, so the greeting and menu drawer look consistent —
            // UNLESS the person already manually saved their own custom
            // name via the profile edit button, in which case that
            // choice always wins over whatever Google's account name is.
            try {
                const current = JSON.parse(localStorage.getItem('notewav_profile') || '{}');
                if (!current.nameManuallySet) {
                    current.name = data.name || current.name;
                }
                if (data.picture) current.avatarDataUrl = data.picture;
                localStorage.setItem('notewav_profile', JSON.stringify(current));
                const nameDisplayEl = document.getElementById('profile-name-display');
                if (nameDisplayEl) nameDisplayEl.textContent = current.name || 'ඔබේ නම (Your name)';
                const nameInputEl = document.getElementById('profile-name-input');
                if (nameInputEl && current.name) nameInputEl.value = current.name;
            } catch (e) { /* ignore */ }
            if (typeof updateGreeting === 'function') updateGreeting();
        } else {
            if (signinBtn) signinBtn.classList.remove('hidden');
            if (signedInInfo) signedInInfo.classList.add('hidden');
        }
    } catch (e) {
        console.warn('Could not check Google auth status:', e);
    }
}

// Pushes the CURRENT coins/streak values up to the server — a no-op
// (server just replies 'ignored') for guests, so this is always safe
// to call regardless of login state.
function syncAccountToServer(partial) {
    if (!notewavIsLoggedIn) return;
    fetch('/user/sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(partial),
    }).catch(() => { /* best-effort only */ });
}

document.addEventListener('DOMContentLoaded', checkGoogleAuthStatus);

document.addEventListener('DOMContentLoaded', function() {
    const loginModalBackdrop = document.getElementById('login-required-modal-backdrop');
    const loginModalCloseBtn = document.getElementById('login-required-close-btn');
    if (loginModalCloseBtn && loginModalBackdrop) {
        loginModalCloseBtn.addEventListener('click', () => loginModalBackdrop.classList.add('hidden'));
    }
    if (loginModalBackdrop) {
        loginModalBackdrop.addEventListener('click', function(e) {
            if (e.target === loginModalBackdrop) loginModalBackdrop.classList.add('hidden');
        });
    }
});

document.addEventListener('DOMContentLoaded', function() {
    trackUsageEvent('app_opened');
});

document.addEventListener('DOMContentLoaded', function() {
    // ===== Output Language Toggle (podcast script/audio language) =====
    // NEW: lets the person explicitly choose whether the generated
    // podcast script (and therefore the audio, since TTS auto-detects
    // language from the final text) comes out in Sinhala or English —
    // independent of whatever language they typed the note in. Only
    // affects Smart Study mode (Full Text Mode passes the original
    // text through untouched, so there's nothing to translate there).
    let outputLanguage = 'si';
    const outputLangSiBtn = document.getElementById('output-lang-si-btn');
    const outputLangEnBtn = document.getElementById('output-lang-en-btn');

    function setOutputLanguageUI(lang) {
        outputLanguage = lang;
        if (outputLangSiBtn) outputLangSiBtn.classList.toggle('active', lang === 'si');
        if (outputLangEnBtn) outputLangEnBtn.classList.toggle('active', lang === 'en');
        try {
            localStorage.setItem('notewav_output_language', lang);
        } catch (e) {
            console.warn('Could not save output language preference:', e);
        }
    }

    try {
        const savedOutputLang = localStorage.getItem('notewav_output_language');
        if (savedOutputLang === 'si' || savedOutputLang === 'en') {
            setOutputLanguageUI(savedOutputLang);
        }
    } catch (e) {
        // ignore, default to 'si'
    }

    if (outputLangSiBtn) outputLangSiBtn.addEventListener('click', () => setOutputLanguageUI('si'));
    if (outputLangEnBtn) outputLangEnBtn.addEventListener('click', () => setOutputLanguageUI('en'));

    // ===== TTS Voice Engine (Standard gTTS vs Natural Gemini AI) =====
    let ttsEngine = 'gtts';
    let ttsModelVersion = 'v25';
    const ttsEngineGttsBtn = document.getElementById('tts-engine-gtts-btn');
    const ttsEngineGeminiBtn = document.getElementById('tts-engine-gemini-btn');
    const ttsVoicePickerWrap = document.getElementById('tts-voice-picker-wrap');
    const ttsVoiceSelect = document.getElementById('tts-voice-select');
    const ttsModelPickerWrap = document.getElementById('tts-model-picker-wrap');
    const ttsModelV25Btn = document.getElementById('tts-model-v25-btn');
    const ttsModelV31Btn = document.getElementById('tts-model-v31-btn');
    const ttsModelV25Hint = document.getElementById('tts-model-v25-hint');
    const ttsModelV31Hint = document.getElementById('tts-model-v31-hint');

    function setTtsEngineUI(engine) {
        ttsEngine = engine;
        if (ttsEngineGttsBtn) ttsEngineGttsBtn.classList.toggle('active', engine === 'gtts');
        if (ttsEngineGeminiBtn) ttsEngineGeminiBtn.classList.toggle('active', engine === 'gemini');
        if (ttsVoicePickerWrap) ttsVoicePickerWrap.classList.toggle('hidden', engine !== 'gemini');
        if (ttsModelPickerWrap) ttsModelPickerWrap.classList.toggle('hidden', engine !== 'gemini');
        updateGeminiTrialStatus();
        try {
            localStorage.setItem('notewav_tts_engine', engine);
        } catch (e) {
            console.warn('Could not save TTS engine preference:', e);
        }
    }

    function setTtsModelVersionUI(version) {
        ttsModelVersion = version;
        if (ttsModelV25Btn) ttsModelV25Btn.classList.toggle('active', version === 'v25');
        if (ttsModelV31Btn) ttsModelV31Btn.classList.toggle('active', version === 'v31');
        if (ttsModelV25Hint) ttsModelV25Hint.classList.toggle('hidden', version !== 'v25');
        if (ttsModelV31Hint) ttsModelV31Hint.classList.toggle('hidden', version !== 'v31');
        try {
            localStorage.setItem('notewav_tts_model_version', version);
        } catch (e) {
            console.warn('Could not save TTS model version preference:', e);
        }
    }

    function updateGeminiTrialStatus() {
        const statusEl = document.getElementById('gemini-trial-status');
        if (!statusEl) return;
        if (ttsEngine !== 'gemini') {
            statusEl.classList.add('hidden');
            return;
        }
        const freeLeft = (typeof getGeminiFreeTrialsLeft === 'function') ? getGeminiFreeTrialsLeft() : 0;
        if (freeLeft > 0) {
            statusEl.classList.remove('hidden');
            statusEl.textContent = `🎁 Free trials ඉතුරුයි: ${freeLeft}/${GEMINI_FREE_TRIALS_TOTAL}`;
        } else {
            // NEW: never show the coin price/balance — the exact
            // pricing tiers are an internal business detail, not
            // something to expose to the person.
            statusEl.classList.add('hidden');
        }
    }

    try {
        const savedTtsEngine = localStorage.getItem('notewav_tts_engine');
        if (savedTtsEngine === 'gtts' || savedTtsEngine === 'gemini') {
            setTtsEngineUI(savedTtsEngine);
        }
        const savedTtsVoice = localStorage.getItem('notewav_tts_voice');
        if (savedTtsVoice && ttsVoiceSelect) {
            ttsVoiceSelect.value = savedTtsVoice;
        }
        const savedTtsModelVersion = localStorage.getItem('notewav_tts_model_version');
        if (savedTtsModelVersion === 'v25' || savedTtsModelVersion === 'v31') {
            setTtsModelVersionUI(savedTtsModelVersion);
        }
    } catch (e) {
        // ignore, default to 'gtts' / first voice option / v25
    }

    if (ttsEngineGttsBtn) ttsEngineGttsBtn.addEventListener('click', () => setTtsEngineUI('gtts'));
    if (ttsEngineGeminiBtn) ttsEngineGeminiBtn.addEventListener('click', () => setTtsEngineUI('gemini'));
    if (ttsModelV25Btn) ttsModelV25Btn.addEventListener('click', () => setTtsModelVersionUI('v25'));
    if (ttsModelV31Btn) ttsModelV31Btn.addEventListener('click', () => setTtsModelVersionUI('v31'));
    if (ttsVoiceSelect) {
        ttsVoiceSelect.addEventListener('change', function() {
            try {
                localStorage.setItem('notewav_tts_voice', this.value);
            } catch (e) {
                console.warn('Could not save TTS voice preference:', e);
            }
        });
    }

    // ===== DOM Elements =====
    const noteInput = document.getElementById('note-input');
    const processBtn = document.getElementById('process-btn');
    const scriptOutput = document.getElementById('script-output');
    const safetySection = document.getElementById('safety-section');
    const audioSection = document.getElementById('audio-section');
    const mindmapSection = document.getElementById('mindmap-section');
    const mindmapContainer = document.getElementById('mindmap-container');
    const generateAudioBtn = document.getElementById('generate-audio-btn');
    const playPauseBtn = document.getElementById('play-pause-btn');
    const stopBtn = document.getElementById('stop-btn');
    const skipBackBtn = document.getElementById('skip-back-btn');
    const skipForwardBtn = document.getElementById('skip-forward-btn');
    const downloadBtn = document.getElementById('download-btn');
    const resetBtn = document.getElementById('reset-btn');
    const highlightContainer = document.getElementById('highlight-text-container');
    const charCount = document.getElementById('char-count');
    const characterCountEl = document.querySelector('.character-count');
    const errorBanner = document.getElementById('error-banner');
    const errorBannerText = document.getElementById('error-banner-text');
    const noteInputClearBtn = document.getElementById('note-input-clear-btn');
    const noteInputCopyBtn = document.getElementById('note-input-copy-btn');
    const scriptOutputCopyBtn = document.getElementById('script-output-copy-btn');

    // ===== Library Elements =====
    const openLibraryBtn = document.getElementById('open-library-btn');
    const libraryModalBackdrop = document.getElementById('library-modal-backdrop');
    const libraryModalClose = document.getElementById('library-modal-close');
    const libraryModalBody = document.getElementById('library-modal-body');
    const librarySubjectInput = document.getElementById('library-subject-input');
    const saveToLibraryBtn = document.getElementById('save-to-library-btn');

    // ===== Share Elements =====
    const shareBtn = document.getElementById('share-btn');
    const shareMenu = document.getElementById('share-menu');
    const shareWhatsappBtn = document.getElementById('share-whatsapp-btn');
    const shareTelegramBtn = document.getElementById('share-telegram-btn');

    // ===== Audio Player Elements =====
    const currentTimeEl = document.getElementById('current-time');
    const totalTimeEl = document.getElementById('total-time');
    const progressFill = document.getElementById('progress-fill');
    const progressBar = document.getElementById('progress-bar');

    // ===== Image Upload Elements =====
    const uploadArea = document.getElementById('upload-area');
    const imageInput = document.getElementById('image-input');
    const cameraInput = document.getElementById('camera-input');
    const ocrStatus = document.getElementById('ocr-status');

    // ===== Playback Speed / Volume Controls =====
    const speedButtons = document.querySelectorAll('.speed-btn');
    const volumeSlider = document.getElementById('volume-slider');

    const SPEED_STORAGE_KEY = 'notewav_preferred_speed';
    try {
        const savedSpeed = localStorage.getItem(SPEED_STORAGE_KEY);
        if (savedSpeed) {
            const savedSpeedNum = parseFloat(savedSpeed);
            const matchingBtn = Array.from(speedButtons).find(b => parseFloat(b.dataset.speed) === savedSpeedNum);
            if (matchingBtn) {
                playbackSpeed = savedSpeedNum;
                speedButtons.forEach(b => b.classList.remove('active'));
                matchingBtn.classList.add('active');
            }
        }
    } catch (e) {
        console.warn('Could not read saved playback speed:', e);
    }

    speedButtons.forEach(btn => {
        btn.addEventListener('click', function() {
            playbackSpeed = parseFloat(this.dataset.speed);
            speedButtons.forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            // Gemini (Natural AI) audio stays locked to its own default
            // pace — the speed slider only affects Standard (gTTS) audio.
            if (audio) audio.playbackRate = getEffectivePlaybackRate();
            try {
                localStorage.setItem(SPEED_STORAGE_KEY, String(playbackSpeed));
            } catch (e) {
                console.warn('Could not save playback speed preference:', e);
            }
        });
    });

    if (volumeSlider) {
        volumeSlider.addEventListener('input', function() {
            playbackVolume = this.value / 100;
            if (audio) audio.volume = playbackVolume;
        });
    }

    // ===== State =====
    let audio = null;
    let currentAudioEngine = 'gtts'; // tracks which engine generated the current `audio`, so the speed slider knows whether to apply
    let isPlaying = false;
    let isOCRRunning = false;
    let highlightUnits = [];
    let playbackSpeed = 1;
    let playbackVolume = 1;

    const SPEED_BOOST_MULTIPLIER = 1.15;
    function getEffectivePlaybackRate() {
        // gTTS (Standard) keeps its EXACT existing behavior unchanged —
        // "1x" has always actually played at 1.15x, because gTTS's
        // mechanical voice sounds noticeably duller/slower at a true
        // 1.0x than a natural human voice does.
        //
        // Gemini (Natural AI) already has its own carefully-paced,
        // natural-sounding delivery, so it does NOT get that artificial
        // boost — its "1x" means a genuine, unmodified 1.0x, and the
        // speed buttons scale from that true baseline.
        if (currentAudioEngine === 'gemini') {
            return playbackSpeed;
        }
        return playbackSpeed * SPEED_BOOST_MULTIPLIER;
    }

    const MAX_TEXT_LENGTH = 2000;

    // ===== Web Audio API state for the audio-reactive waveform =====
    let audioCtx = null;
    let analyser = null;
    let sourceNode = null;
    let waveformData = null;
    let waveformAnimationId = null;
    const waveBarEls = document.querySelectorAll('.wave-bar');
    const playerVisualizerEl = document.querySelector('.player-visualizer');

    // ========================================
    // ERROR BANNER (Feature 2: friendly error UI)
    // ========================================
    function showErrorBanner(message) {
        if (!errorBanner || !errorBannerText) {
            alert(message);
            return;
        }
        errorBannerText.textContent = message;
        errorBanner.classList.remove('hidden');
        clearTimeout(showErrorBanner._timer);
        showErrorBanner._timer = setTimeout(() => {
            errorBanner.classList.add('hidden');
        }, 7000);
    }

    // ========================================
    // CHARACTER COUNTER (with limit warning)
    // ========================================
    noteInput.addEventListener('input', function() {
        const len = this.value.length;
        charCount.textContent = len;
        if (characterCountEl) {
            characterCountEl.classList.toggle('over-limit', len > MAX_TEXT_LENGTH);
        }
    });

    // ========================================
    // TEXTAREA COPY / CLEAR ACTIONS
    // ========================================
    function showCopiedFeedback(btn) {
        const icon = btn.querySelector('i');
        const originalClass = icon.className;
        icon.className = 'fas fa-check';
        btn.classList.add('copied');
        setTimeout(() => {
            icon.className = originalClass;
            btn.classList.remove('copied');
        }, 1500);
    }

    async function copyTextareaContent(textarea, btn) {
        const text = textarea.value.trim();
        if (!text) {
            showErrorBanner('Copy කරන්න මොකුත් නෑ.');
            return;
        }
        try {
            await navigator.clipboard.writeText(text);
            showCopiedFeedback(btn);
        } catch (err) {
            console.error('Copy failed:', err);
            textarea.select();
            document.execCommand('copy');
            showCopiedFeedback(btn);
        }
    }

    if (noteInputCopyBtn) {
        noteInputCopyBtn.addEventListener('click', () => copyTextareaContent(noteInput, noteInputCopyBtn));
    }
    if (scriptOutputCopyBtn) {
        scriptOutputCopyBtn.addEventListener('click', () => copyTextareaContent(scriptOutput, scriptOutputCopyBtn));
    }
    if (noteInputClearBtn) {
        noteInputClearBtn.addEventListener('click', function() {
            noteInput.value = '';
            noteInput.dispatchEvent(new Event('input'));
            noteInput.style.height = 'auto';
            noteInput.focus();
        });
    }

    // ========================================
    // NOTES LIBRARY (save / list / load / delete)
    // ========================================
    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    function openLibraryModal() {
        if (!libraryModalBackdrop || !libraryModalBody) return;
        libraryModalBackdrop.classList.remove('hidden');
        loadLibraryList();
    }

    function closeLibraryModal() {
        if (libraryModalBackdrop) libraryModalBackdrop.classList.add('hidden');
    }

    let allLibraryNotes = [];
    const librarySearchInput = document.getElementById('library-search-input');
    let librarySearchDebounceTimer = null;

    async function loadLibraryList(query) {
        libraryModalBody.innerHTML = '<p class="mindmap-empty"><span class="mini-wave"><span></span><span></span><span></span><span></span></span> Loading...</p>';
        if (!query && librarySearchInput) librarySearchInput.value = '';
        try {
            const url = query ? `/library/notes?q=${encodeURIComponent(query)}` : '/library/notes';
            const response = await fetch(url);
            const data = await response.json();
            if (data.status !== 'success') {
                libraryModalBody.innerHTML = '<p class="mindmap-empty">Library එක load කරගැනීම අසාර්ථක විය.</p>';
                return;
            }
            allLibraryNotes = data.notes || [];
            renderLibraryList(allLibraryNotes, query);
        } catch (err) {
            console.error('Library load error:', err);
            libraryModalBody.innerHTML = '<p class="mindmap-empty">Library එක load කරගැනීම අසාර්ථක විය.</p>';
        }
    }

    // NEW: search now goes through the backend (debounced) instead of
    // only filtering title/subject already loaded in the browser — this
    // means it searches the FULL saved note content too, not just the
    // title/subject shown in the list.
    if (librarySearchInput) {
        librarySearchInput.addEventListener('input', function() {
            const term = this.value;
            clearTimeout(librarySearchDebounceTimer);
            librarySearchDebounceTimer = setTimeout(() => {
                loadLibraryList(term.trim());
            }, 350);
        });
    }

    let combineModeActive = false;
    let combineSelectedIds = new Set();

    function renderLibraryList(notes, searchTerm) {
        if (!notes.length) {
            const message = searchTerm
                ? `"${escapeHtml(searchTerm)}" එකට notes කිසිවක් හම්බුනේ නෑ.`
                : 'තවම save කරපු notes නෑ. Script එකක් process කරලා "Library එකට Save කරන්න" click කරන්න.';
            libraryModalBody.innerHTML = `<p class="mindmap-empty">${message}</p>`;
            return;
        }

        const grouped = {};
        notes.forEach(note => {
            const subject = note.subject || 'General';
            if (!grouped[subject]) grouped[subject] = [];
            grouped[subject].push(note);
        });

        let html = '';
        Object.keys(grouped).sort().forEach(subject => {
            html += `<div class="library-subject-group">`;
            html += `<div class="library-subject-heading">${escapeHtml(subject)} (${grouped[subject].length})</div>`;
            grouped[subject].forEach(note => {
                const dateStr = new Date(note.created_at).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
                const isChecked = combineSelectedIds.has(String(note.id));
                html += `
                    <div class="library-note-item">
                        ${combineModeActive ? `<input type="checkbox" class="library-combine-checkbox" data-id="${note.id}" style="margin-right: 10px; width: 18px; height: 18px; accent-color: #6b30ff; flex-shrink: 0;" ${isChecked ? 'checked' : ''}>` : ''}
                        <div class="library-note-info">
                            <div class="library-note-title">${escapeHtml(note.title)}</div>
                            <div class="library-note-date">${dateStr}</div>
                        </div>
                        <div class="library-note-actions">
                            <button class="library-load-btn" data-id="${note.id}" aria-label="Load"><i class="fas fa-folder-open"></i></button>
                            <button class="library-delete-btn" data-id="${note.id}" aria-label="Delete"><i class="fas fa-trash"></i></button>
                        </div>
                    </div>`;
            });
            html += `</div>`;
        });

        libraryModalBody.innerHTML = html;

        libraryModalBody.querySelectorAll('.library-load-btn').forEach(btn => {
            btn.addEventListener('click', () => loadLibraryNote(btn.dataset.id));
        });
        libraryModalBody.querySelectorAll('.library-delete-btn').forEach(btn => {
            btn.addEventListener('click', () => deleteLibraryNote(btn.dataset.id));
        });
        libraryModalBody.querySelectorAll('.library-combine-checkbox').forEach(cb => {
            cb.addEventListener('change', function() {
                if (this.checked) combineSelectedIds.add(this.dataset.id);
                else combineSelectedIds.delete(this.dataset.id);
                updateCombineGenerateButton();
            });
        });
    }

    function updateCombineGenerateButton() {
        const combineGenerateBtn = document.getElementById('library-combine-generate-btn');
        const combineGenerateLabel = document.getElementById('library-combine-generate-label');
        if (!combineGenerateBtn) return;
        const count = combineSelectedIds.size;
        if (combineModeActive && count >= 2) {
            combineGenerateBtn.classList.remove('hidden');
            if (combineGenerateLabel) combineGenerateLabel.textContent = `Combine & Generate Audio (${count} notes)`;
        } else {
            combineGenerateBtn.classList.add('hidden');
        }
    }

    const libraryCombineToggleBtn = document.getElementById('library-combine-toggle-btn');
    const libraryCombineHint = document.getElementById('library-combine-hint');
    if (libraryCombineToggleBtn) {
        libraryCombineToggleBtn.addEventListener('click', function() {
            combineModeActive = !combineModeActive;
            combineSelectedIds.clear();
            this.classList.toggle('active-toggle', combineModeActive);
            if (libraryCombineHint) libraryCombineHint.classList.toggle('hidden', !combineModeActive);
            updateCombineGenerateButton();
            renderLibraryList(allLibraryNotes);
        });
    }

    const libraryCombineGenerateBtn = document.getElementById('library-combine-generate-btn');
    if (libraryCombineGenerateBtn) {
        libraryCombineGenerateBtn.addEventListener('click', async function() {
            const ids = Array.from(combineSelectedIds);
            if (ids.length < 2) return;

            this.disabled = true;
            this.innerHTML = '<span class="mini-wave"><span></span><span></span><span></span><span></span></span> Combine වෙමින්...';

            try {
                const noteTexts = [];
                for (const id of ids) {
                    const res = await fetch(`/library/notes/${id}`);
                    const data = await res.json();
                    if (data.status === 'success' && data.note) {
                        noteTexts.push(data.note.processed_text || data.note.note_text || '');
                    }
                }
                const combinedText = noteTexts.filter(Boolean).join('\n\n');
                if (!combinedText) {
                    showErrorBanner('Combine කරන්න content එකක් හම්බුනේ නෑ.');
                    return;
                }

                // Reuse the normal script/safety-check flow — drop the
                // combined text straight into it, exactly as if it had
                // just been processed, so the existing "Generate Audio"
                // button and pipeline handle it unchanged.
                if (scriptOutput) scriptOutput.value = combinedText;
                safetySection.classList.remove('hidden');
                audioSection.classList.add('hidden');
                if (mindmapSection) mindmapSection.classList.add('hidden'); // no combined mind map — audio-only feature
                const quizSectionCombineEl = document.getElementById('quiz-section');
                if (quizSectionCombineEl) quizSectionCombineEl.classList.add('hidden');

                closeLibraryModal();
                combineModeActive = false;
                combineSelectedIds.clear();
                setTimeout(() => {
                    safetySection.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }, 300);
            } catch (err) {
                console.error('Combine error:', err);
                showErrorBanner('Notes combine කරගැනීම අසාර්ථක විය.');
            } finally {
                this.disabled = false;
                this.innerHTML = '<i class="fas fa-headphones"></i> <span id="library-combine-generate-label">Combine & Generate Audio</span>';
            }
        });
    }

    async function loadLibraryNote(id) {
        try {
            const response = await fetch(`/library/notes/${id}`);
            const data = await response.json();
            if (data.status !== 'success') {
                showErrorBanner('Note එක load කරගැනීම අසාර්ථක විය.');
                return;
            }
            const note = data.note;
            noteInput.value = note.note_text || '';
            noteInput.dispatchEvent(new Event('input'));
            autoResizeTextarea(noteInput);

            if (note.processed_text) {
                scriptOutput.value = note.processed_text;
                safetySection.classList.remove('hidden');
                await renderMindMap(note.mermaid_code_si, note.mermaid_code_en);
            } else {
                safetySection.classList.add('hidden');
                if (mindmapSection) mindmapSection.classList.add('hidden');
            }

            audioSection.classList.add('hidden');
            closeLibraryModal();
            window.scrollTo({ top: 0, behavior: 'smooth' });
        } catch (err) {
            console.error('Library note load error:', err);
            showErrorBanner('Note එක load කරගැනීම අසාර්ථක විය.');
        }
    }

    async function deleteLibraryNote(id) {
        if (!confirm('මෙම note එක permanently delete කරන්නද?')) return;
        try {
            const response = await fetch(`/library/notes/${id}`, { method: 'DELETE' });
            const data = await response.json();
            if (data.status === 'success') {
                loadLibraryList();
            } else {
                showErrorBanner('Delete කිරීම අසාර්ථක විය.');
            }
        } catch (err) {
            console.error('Library delete error:', err);
            showErrorBanner('Delete කිරීම අසාර්ථක විය.');
        }
    }

    if (openLibraryBtn) openLibraryBtn.addEventListener('click', openLibraryModal);
    if (libraryModalClose) libraryModalClose.addEventListener('click', closeLibraryModal);
    if (libraryModalBackdrop) {
        libraryModalBackdrop.addEventListener('click', function(e) {
            if (e.target === libraryModalBackdrop) closeLibraryModal();
        });
    }

    // ===== Side Menu Drawer (Settings, opens from the left) =====
    const openMenuBtn = document.getElementById('open-menu-btn');
    const menuDrawerBackdrop = document.getElementById('menu-drawer-backdrop');
    const menuDrawerClose = document.getElementById('menu-drawer-close');
    if (openMenuBtn && menuDrawerBackdrop) {
        openMenuBtn.addEventListener('click', function() {
            menuDrawerBackdrop.classList.add('open');
        });
    }
    if (menuDrawerClose && menuDrawerBackdrop) {
        menuDrawerClose.addEventListener('click', function() {
            menuDrawerBackdrop.classList.remove('open');
        });
    }
    if (menuDrawerBackdrop) {
        menuDrawerBackdrop.addEventListener('click', function(e) {
            if (e.target === menuDrawerBackdrop) menuDrawerBackdrop.classList.remove('open');
        });
    }

    if (saveToLibraryBtn) {
        saveToLibraryBtn.addEventListener('click', async function() {
            const noteText = noteInput.value.trim();
            if (!noteText) {
                showErrorBanner('Save කරන්න note එකක් නෑ.');
                return;
            }
            // NEW: Library saving now requires being signed in with
            // Google. Guests get a friendly prompt instead of the
            // save silently going nowhere.
            if (typeof notewavIsLoggedIn !== 'undefined' && !notewavIsLoggedIn) {
                const loginModal = document.getElementById('login-required-modal-backdrop');
                if (loginModal) loginModal.classList.remove('hidden');
                return;
            }
            const subject = (librarySubjectInput.value || 'General').trim() || 'General';
            const mode = document.querySelector('input[name="study_mode"]:checked').value;

            saveToLibraryBtn.disabled = true;
            saveToLibraryBtn.innerHTML = '<span class="mini-wave"><span></span><span></span><span></span><span></span></span> Saving...';

            try {
                let profileName = '';
                try {
                    const profile = JSON.parse(localStorage.getItem('notewav_profile') || '{}');
                    if (profile.name) profileName = profile.name.trim();
                } catch (e) { /* ignore */ }

                const response = await fetch('/library/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        subject,
                        note_text: noteText,
                        processed_text: scriptOutput.value || '',
                        mermaid_code_si: mermaidCodes.si || '',
                        mermaid_code_en: mermaidCodes.en || '',
                        mode,
                        anon_id: (typeof getOrCreateAnonId === 'function') ? getOrCreateAnonId() : '',
                        user_name: profileName,
                    }),
                });
                const data = await response.json();
                if (data.status === 'success') {
                    saveToLibraryBtn.innerHTML = '<i class="fas fa-check"></i> Saved!';
                    setTimeout(() => {
                        saveToLibraryBtn.innerHTML = '<i class="fas fa-bookmark"></i> Library එකට Save කරන්න';
                    }, 2000);
                } else {
                    showErrorBanner(data.message || 'Save කිරීම අසාර්ථක විය.');
                    saveToLibraryBtn.innerHTML = '<i class="fas fa-bookmark"></i> Library එකට Save කරන්න';
                }
            } catch (err) {
                console.error('Library save error:', err);
                showErrorBanner('Network error. Save කිරීම අසාර්ථක විය.');
                saveToLibraryBtn.innerHTML = '<i class="fas fa-bookmark"></i> Library එකට Save කරන්න';
            } finally {
                saveToLibraryBtn.disabled = false;
            }
        });
    }

    // ========================================
    // SHARE (WhatsApp / Telegram)
    // ========================================
    if (shareBtn && shareMenu) {
        shareBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            shareMenu.classList.toggle('hidden');
        });
        document.addEventListener('click', function(e) {
            if (!shareMenu.classList.contains('hidden') && !shareMenu.contains(e.target) && e.target !== shareBtn) {
                shareMenu.classList.add('hidden');
            }
        });
    }

    const APP_SHARE_URL = 'https://notewav.onrender.com';

    function getShareText() {
        const script = (scriptOutput.value || '').trim();
        const snippet = script.length > 300 ? script.slice(0, 300) + '...' : script;
        return `🎙️ NoteWav AI වලින් හදපු study note එකක්:\n\n${snippet}\n\n👉 ඔයත් try කරන්න: ${APP_SHARE_URL}`;
    }

    if (shareWhatsappBtn) {
        shareWhatsappBtn.addEventListener('click', function() {
            const text = encodeURIComponent(getShareText());
            window.open(`https://wa.me/?text=${text}`, '_blank');
            shareMenu.classList.add('hidden');
        });
    }
    if (shareTelegramBtn) {
        shareTelegramBtn.addEventListener('click', function() {
            const text = encodeURIComponent(getShareText());
            window.open(`https://t.me/share/url?url=&text=${text}`, '_blank');
            shareMenu.classList.add('hidden');
        });
    }

    const shareWithAudioBtn = document.getElementById('share-with-audio-btn');
    if (shareWithAudioBtn) {
        shareWithAudioBtn.addEventListener('click', async function() {
            shareMenu.classList.add('hidden');
            if (!audio || !audio.src) {
                showErrorBanner('Share කරන්න Audio එකක් නෑ — කලින් Audio එකක් Generate කරන්න.');
                return;
            }
            if (!navigator.share || !navigator.canShare) {
                showErrorBanner('මේ browser එකේ Audio file share කිරීම support කරන්නේ නෑ — WhatsApp/Telegram (Text) option එක try කරන්න.');
                return;
            }
            try {
                const response = await fetch(audio.src);
                const audioBlob = await response.blob();
                const audioFile = new File([audioBlob], 'notewav_audio.mp3', { type: 'audio/mpeg' });

                if (navigator.canShare({ files: [audioFile] })) {
                    await navigator.share({
                        files: [audioFile],
                        title: 'NoteWav AI',
                        text: getShareText(),
                    });
                } else {
                    showErrorBanner('මේ device එකේ Audio file share කිරීම support කරන්නේ නෑ.');
                }
            } catch (shareErr) {
                if (shareErr && shareErr.name !== 'AbortError') {
                    console.error('Audio share failed:', shareErr);
                    showErrorBanner('Audio share කරගැනීම අසාර්ථක විය: ' + shareErr.message);
                }
            }
        });
    }

    // ========================================
    // FORMAT TIME
    // ========================================
    function formatTime(seconds) {
        if (!seconds || isNaN(seconds) || !isFinite(seconds)) {
            return '0:00';
        }
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    }

    // ========================================
    // UPDATE PROGRESS
    // ========================================
    function updateProgress(percentage) {
        if (!progressFill) return;
        const clamped = Math.min(100, Math.max(0, percentage));
        progressFill.style.width = `${clamped}%`;
    }

    // ========================================
    // AUDIO-REACTIVE WAVEFORM (Web Audio API)
    // ========================================
    function setupAudioAnalyser(audioEl) {
        try {
            if (!window.AudioContext && !window.webkitAudioContext) {
                return false;
            }
            if (!audioCtx) {
                audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            }
            if (sourceNode) {
                try { sourceNode.disconnect(); } catch (err) { /* already gone */ }
            }
            sourceNode = audioCtx.createMediaElementSource(audioEl);
            analyser = audioCtx.createAnalyser();
            analyser.fftSize = 64;
            analyser.smoothingTimeConstant = 0.75;
            sourceNode.connect(analyser);
            analyser.connect(audioCtx.destination);
            waveformData = new Uint8Array(analyser.frequencyBinCount);
            return true;
        } catch (err) {
            console.warn('Web Audio analyser unavailable, using CSS fallback animation:', err);
            analyser = null;
            return false;
        }
    }

    function animateWaveform() {
        if (!analyser || !waveformData) return;
        analyser.getByteFrequencyData(waveformData);

        const barCount = waveBarEls.length;
        for (let i = 0; i < barCount; i++) {
            const dataIndex = Math.floor((i / barCount) * waveformData.length);
            const value = waveformData[dataIndex] / 255;
            const scale = 0.35 + value * 1.15;
            const bar = waveBarEls[i];
            bar.style.transform = `scaleY(${scale.toFixed(2)})`;
            bar.style.opacity = (0.5 + value * 0.5).toFixed(2);
        }

        waveformAnimationId = requestAnimationFrame(animateWaveform);
    }

    function startWaveformAnimation() {
        if (!analyser) return;
        if (playerVisualizerEl) playerVisualizerEl.classList.add('audio-reactive');
        if (waveformAnimationId) cancelAnimationFrame(waveformAnimationId);
        animateWaveform();
    }

    function stopWaveformAnimation() {
        if (waveformAnimationId) {
            cancelAnimationFrame(waveformAnimationId);
            waveformAnimationId = null;
        }
        if (playerVisualizerEl) playerVisualizerEl.classList.remove('audio-reactive');
        waveBarEls.forEach(bar => {
            bar.style.transform = '';
            bar.style.opacity = '';
        });
    }

    // ========================================
    // SPLIT TEXT INTO LINES FOR HIGHLIGHTING
    // ========================================
    function splitTextIntoLines(text) {
        if (!text) return [];
        const lines = text.split(/[.!?।\n]+/).filter(line => line.trim().length > 0);
        return lines.map(line => line.trim());
    }

    // ========================================
    // UPDATE HIGHLIGHT BASED ON CURRENT TIME
    // ========================================
    function updateHighlight(currentTime) {
        if (!highlightUnits || highlightUnits.length === 0) return;

        let activeIndex = -1;
        for (let i = 0; i < highlightUnits.length; i++) {
            if (currentTime >= highlightUnits[i].start && currentTime < highlightUnits[i].end) {
                activeIndex = i;
                break;
            }
        }
        if (activeIndex === -1) {
            for (let i = highlightUnits.length - 1; i >= 0; i--) {
                if (currentTime >= highlightUnits[i].start) {
                    activeIndex = i;
                    break;
                }
            }
        }
        if (activeIndex === -1) return;

        const unitElements = highlightContainer.querySelectorAll('.lyric-line');
        unitElements.forEach(el => el.classList.remove('active'));
        if (unitElements[activeIndex]) {
            unitElements[activeIndex].classList.add('active');
            unitElements[activeIndex].scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }

    // ========================================
    // RENDER LYRICS AS WORD-PAIR HIGHLIGHT UNITS
    // ========================================
    function renderLyrics(text, sentenceTimings) {
        if (!text) {
            highlightContainer.innerHTML = '';
            highlightUnits = [];
            return;
        }

        highlightUnits = [];

        if (Array.isArray(sentenceTimings) && sentenceTimings.length > 0) {
            sentenceTimings.forEach(sentence => {
                const words = sentence.text.trim().split(/\s+/).filter(Boolean);
                if (words.length === 0) return;

                const pairs = [];
                for (let i = 0; i < words.length; i += 2) {
                    pairs.push(words.slice(i, i + 2).join(' '));
                }

                const sentenceDuration = sentence.end - sentence.start;
                const totalChars = pairs.reduce((sum, p) => sum + p.length, 0) || 1;
                let cursor = sentence.start;

                pairs.forEach(pairText => {
                    const share = pairText.length / totalChars;
                    const duration = sentenceDuration * share;
                    highlightUnits.push({
                        text: pairText,
                        start: cursor,
                        end: cursor + duration,
                    });
                    cursor += duration;
                });
            });
        }

        if (highlightUnits.length === 0) {
            const lines = splitTextIntoLines(text);
            highlightUnits = lines.map((line, i) => ({ text: line, start: i, end: i + 1, _isFallback: true }));
        }

        if (highlightUnits.length === 0) {
            highlightContainer.innerHTML = `<p>${text}</p>`;
            return;
        }

        let html = '';
        highlightUnits.forEach((unit, index) => {
            html += `<span class="lyric-line" data-index="${index}">${unit.text}</span> `;
        });

        highlightContainer.innerHTML = html;
    }

    if (highlightContainer) {
        highlightContainer.addEventListener('click', function(e) {
            const lineEl = e.target.closest('.lyric-line');
            if (!lineEl || !audio || !highlightUnits || highlightUnits.length === 0) return;
            const index = parseInt(lineEl.dataset.index, 10);
            if (isNaN(index) || !highlightUnits[index]) return;
            const totalDuration = audio.duration || 0;
            if (!totalDuration) return;

            const unit = highlightUnits[index];
            let seekTime;
            if (unit._isFallback) {
                const timePerUnit = totalDuration / highlightUnits.length;
                seekTime = index * timePerUnit;
            } else {
                seekTime = unit.start;
            }
            audio.currentTime = Math.min(totalDuration, seekTime);

            if (audio.paused) {
                audio.play().catch(() => {});
                isPlaying = true;
                playPauseBtn.innerHTML = '<i class="fas fa-pause"></i>';
                document.querySelector('.player-container').classList.add('playing');
                startWaveformAnimation();
            }
        });
    }

    // ========================================
    // FEATURE 1: SMART MIND MAP (Mermaid.js)
    // ========================================
    if (window.mermaid) {
        mermaid.initialize({
            startOnLoad: false,
            theme: 'dark',
            securityLevel: 'loose',
            fontFamily: 'Plus Jakarta Sans, sans-serif',
            flowchart: {
                htmlLabels: false,
                nodeSpacing: 35,
                rankSpacing: 90,
                padding: 48,
                curve: 'basis'
            },
            themeVariables: {
                fontSize: '19px',
                lineColor: '#8b6fd6'
            },
            themeCSS: `
                .node rect, .node polygon, .node circle, .node ellipse {
                    rx: 14px; ry: 14px;
                    filter: drop-shadow(0 4px 14px rgba(107, 48, 255, 0.28));
                }
                .edgePaths .path, .edgePath .path {
                    stroke-width: 2.5px;
                }
                .edgePath path,
                .edgePaths path,
                path.flowchart-link,
                .flowchart-link,
                .edge-thickness-normal,
                .edge-thickness-thick,
                .edge-pattern-solid,
                .edge-pattern-dashed {
                    stroke: #8b6fd6 !important;
                }
                .node.root-node rect, .node.root-node polygon {
                    filter: drop-shadow(0 6px 22px rgba(107, 48, 255, 0.5));
                }
                marker path,
                .arrowheadPath,
                .flowchart-pointEnd,
                .flowchart-pointStart {
                    fill: #8b6fd6 !important;
                    stroke: #8b6fd6 !important;
                }
            `
        });
    }

    let mermaidCodes = { si: '', en: '' };
    let mindmapSvgCache = { si: '', en: '' };
    let currentMindMapLang = 'si';
    let lastMindMapSvg = '';

    const mindmapLangSiBtn = document.getElementById('mindmap-lang-si-btn');
    const mindmapLangEnBtn = document.getElementById('mindmap-lang-en-btn');

    function setMindMapLangButtonsUI(lang) {
        if (mindmapLangSiBtn) mindmapLangSiBtn.classList.toggle('active', lang === 'si');
        if (mindmapLangEnBtn) mindmapLangEnBtn.classList.toggle('active', lang === 'en');
    }

    function forceEdgeColor(containerEl) {
        if (!containerEl) return;
        const svgEl = containerEl.querySelector('svg');
        if (!svgEl) return;
        svgEl.querySelectorAll('path, line, polyline').forEach(p => {
            if (p.closest('marker')) return;
            p.removeAttribute('style');
            p.setAttribute('stroke', '#8b6fd6');
            p.setAttribute('fill', 'none');
            p.setAttribute('stroke-width', '2.5');
            p.style.setProperty('stroke', '#8b6fd6', 'important');
            p.style.setProperty('fill', 'none', 'important');
            p.style.setProperty('stroke-width', '2.5px', 'important');
        });
    }

    async function renderMindMapForLang(lang) {
        if (!mindmapSection || !mindmapContainer) return;
        currentMindMapLang = lang;
        setMindMapLangButtonsUI(lang);

        const code = mermaidCodes[lang];
        if (!code || !code.trim()) {
            lastMindMapSvg = '';
            mindmapContainer.innerHTML = `<p class="mindmap-empty">${lang === 'si' ? 'සිංහල' : 'English'} Mind Map එකක් generate වුනේ නෑ.</p>`;
            return;
        }

        if (mindmapSvgCache[lang]) {
            lastMindMapSvg = mindmapSvgCache[lang];
            mindmapContainer.innerHTML = lastMindMapSvg;
            forceEdgeColor(mindmapContainer);
            return;
        }

        if (!window.mermaid) {
            mindmapContainer.innerHTML = '<p class="mindmap-empty">Mermaid.js library එක load වුනේ නෑ.</p>';
            return;
        }

        mindmapContainer.innerHTML = '<p class="mindmap-loading"><span class="mini-wave"><span></span><span></span><span></span><span></span></span> Mind map හදමින්...</p>';

        try {
            const uniqueId = 'mermaidGraph_' + lang + '_' + Date.now();
            const { svg } = await mermaid.render(uniqueId, code);
            mindmapSvgCache[lang] = svg;
            if (currentMindMapLang === lang) {
                lastMindMapSvg = svg;
                mindmapContainer.innerHTML = svg;
                forceEdgeColor(mindmapContainer);
            }
        } catch (err) {
            console.error('Mermaid render error:', err);
            mindmapContainer.innerHTML = '<p class="mindmap-empty">Mind map එක render කරගන්න බැරි උනා — audio/script එකට කිසිම බලපෑමක් නෑ.</p>';
        }
    }

    async function renderMindMap(codeSi, codeEn) {
        if (!mindmapSection || !mindmapContainer) return;

        mindmapSection.classList.remove('hidden');
        const quizSectionEl = document.getElementById('quiz-section');
        if (quizSectionEl) quizSectionEl.classList.remove('hidden');
        mermaidCodes = { si: codeSi || '', en: codeEn || '' };
        mindmapSvgCache = { si: '', en: '' };

        await renderMindMapForLang('si');

        if (window.mermaid && mermaidCodes.en && mermaidCodes.en.trim()) {
            try {
                const uniqueId = 'mermaidGraph_en_preload_' + Date.now();
                const { svg } = await mermaid.render(uniqueId, mermaidCodes.en);
                mindmapSvgCache.en = svg;
            } catch (err) {
                console.warn('English mind map pre-render failed (will retry on click):', err);
            }
        }
    }

    if (mindmapLangSiBtn) mindmapLangSiBtn.addEventListener('click', () => renderMindMapForLang('si'));
    if (mindmapLangEnBtn) mindmapLangEnBtn.addEventListener('click', () => renderMindMapForLang('en'));

    // ========================================
    // MIND MAP MODAL (enlarge / close / zoom / PDF export)
    // ========================================
    const mindmapModalBackdrop = document.getElementById('mindmap-modal-backdrop');
    const mindmapModalCard = document.getElementById('mindmap-modal-card');
    const mindmapModalBody = document.getElementById('mindmap-modal-body');
    const mindmapModalClose = document.getElementById('mindmap-modal-close');
    const mindmapZoomWrapper = document.getElementById('mindmap-modal-zoom-wrapper');
    const mindmapZoomLevelEl = document.getElementById('mindmap-zoom-level');
    const mindmapZoomInBtn = document.getElementById('mindmap-zoom-in');
    const mindmapZoomOutBtn = document.getElementById('mindmap-zoom-out');
    const mindmapZoomResetBtn = document.getElementById('mindmap-zoom-reset');
    const mindmapDownloadPdfBtn = document.getElementById('mindmap-download-pdf');
    const mindmapDownloadPngBtn = document.getElementById('mindmap-download-png');

    let mindmapZoom = 1;
    let mindmapNaturalWidth = 0;
    let mindmapNaturalHeight = 0;

    function setMindMapZoom(level) {
        mindmapZoom = Math.min(10, Math.max(0.3, level));
        if (mindmapZoomWrapper && mindmapNaturalWidth && mindmapNaturalHeight) {
            mindmapZoomWrapper.style.width = (mindmapNaturalWidth * mindmapZoom) + 'px';
            mindmapZoomWrapper.style.height = (mindmapNaturalHeight * mindmapZoom) + 'px';
        }
        if (mindmapZoomLevelEl) mindmapZoomLevelEl.textContent = `${Math.round(mindmapZoom * 100)}%`;
    }

    function openMindMapModal() {
        if (!lastMindMapSvg || !mindmapModalBackdrop || !mindmapZoomWrapper) {
            showErrorBanner('විශාල කර බලන්න Mind Map එකක් නෑ.');
            return;
        }
        mindmapZoomWrapper.innerHTML = lastMindMapSvg;
        forceEdgeColor(mindmapZoomWrapper);
        mindmapZoomWrapper.style.transform = '';
        mindmapModalBackdrop.classList.remove('hidden');

        requestAnimationFrame(() => {
            const svgEl = mindmapZoomWrapper.querySelector('svg');
            if (svgEl && mindmapModalBody) {
                const widthAttr = parseFloat(svgEl.getAttribute('width'));
                const heightAttr = parseFloat(svgEl.getAttribute('height'));
                const svgRect = svgEl.getBoundingClientRect();
                mindmapNaturalWidth = widthAttr || svgRect.width;
                mindmapNaturalHeight = heightAttr || svgRect.height;

                const margin = 70;
                const availableWidth = mindmapModalBody.clientWidth - margin;
                const availableHeight = mindmapModalBody.clientHeight - margin;
                if (mindmapNaturalWidth > 0 && mindmapNaturalHeight > 0) {
                    const fitScale = Math.min(
                        availableWidth / mindmapNaturalWidth,
                        availableHeight / mindmapNaturalHeight,
                        2.5
                    );
                    setMindMapZoom(fitScale);
                    return;
                }
            }
            setMindMapZoom(1);
        });
    }

    function closeMindMapModal() {
        if (mindmapModalBackdrop) mindmapModalBackdrop.classList.add('hidden');
    }

    if (mindmapContainer) {
        mindmapContainer.addEventListener('click', openMindMapModal);
        mindmapContainer.addEventListener('keydown', function(e) {
            if (e.code === 'Enter' || e.code === 'Space') {
                e.preventDefault();
                openMindMapModal();
            }
        });
    }

    if (mindmapModalClose) {
        mindmapModalClose.addEventListener('click', closeMindMapModal);
    }

    if (mindmapModalBackdrop) {
        mindmapModalBackdrop.addEventListener('click', function(e) {
            if (e.target === mindmapModalBackdrop) closeMindMapModal();
        });
    }

    document.addEventListener('keydown', function(e) {
        if (e.code === 'Escape' && mindmapModalBackdrop && !mindmapModalBackdrop.classList.contains('hidden')) {
            closeMindMapModal();
        }
    });

    if (mindmapModalBody) {
        mindmapModalBody.addEventListener('wheel', function(e) {
            e.preventDefault();
            const step = 0.15;
            setMindMapZoom(mindmapZoom + (e.deltaY < 0 ? step : -step));
        }, { passive: false });
    }
    if (mindmapZoomInBtn) mindmapZoomInBtn.addEventListener('click', () => setMindMapZoom(mindmapZoom + 0.25));
    if (mindmapZoomOutBtn) mindmapZoomOutBtn.addEventListener('click', () => setMindMapZoom(mindmapZoom - 0.25));
    if (mindmapZoomResetBtn) mindmapZoomResetBtn.addEventListener('click', () => setMindMapZoom(1));

    let isDraggingMindMap = false;
    let mindmapDragStartX = 0;
    let mindmapDragStartY = 0;
    let mindmapScrollStartLeft = 0;
    let mindmapScrollStartTop = 0;

    if (mindmapModalBody) {
        mindmapModalBody.addEventListener('mousedown', function(e) {
            isDraggingMindMap = true;
            mindmapDragStartX = e.pageX;
            mindmapDragStartY = e.pageY;
            mindmapScrollStartLeft = mindmapModalBody.scrollLeft;
            mindmapScrollStartTop = mindmapModalBody.scrollTop;
            mindmapModalBody.classList.add('grabbing');
        });

        window.addEventListener('mousemove', function(e) {
            if (!isDraggingMindMap) return;
            e.preventDefault();
            const dx = e.pageX - mindmapDragStartX;
            const dy = e.pageY - mindmapDragStartY;
            mindmapModalBody.scrollLeft = mindmapScrollStartLeft - dx;
            mindmapModalBody.scrollTop = mindmapScrollStartTop - dy;
        });

        window.addEventListener('mouseup', function() {
            if (!isDraggingMindMap) return;
            isDraggingMindMap = false;
            mindmapModalBody.classList.remove('grabbing');
        });
    }

    function prepareSvgForExport(svgString) {
        const parser = new DOMParser();
        const doc = parser.parseFromString(svgString, 'image/svg+xml');
        const svgEl = doc.documentElement;

        svgEl.querySelectorAll('path, line, polyline').forEach(p => {
            if (p.closest('marker')) return;
            p.removeAttribute('style');
            p.setAttribute('stroke', '#8b6fd6');
            p.setAttribute('fill', 'none');
            p.setAttribute('stroke-width', '2.5');
        });

        let minX = 0, minY = 0, width = 0, height = 0;
        const viewBox = svgEl.getAttribute('viewBox');
        if (viewBox) {
            const parts = viewBox.trim().split(/\s+/).map(Number);
            if (parts.length === 4) {
                minX = parts[0];
                minY = parts[1];
                width = parts[2];
                height = parts[3];
            }
        }
        const widthAttr = svgEl.getAttribute('width');
        const heightAttr = svgEl.getAttribute('height');
        if (widthAttr && !widthAttr.includes('%')) width = parseFloat(widthAttr) || width;
        if (heightAttr && !heightAttr.includes('%')) height = parseFloat(heightAttr) || height;

        if (!width || !height) {
            width = width || 1200;
            height = height || 800;
        }

        const margin = Math.max(70, Math.round(width * 0.05));
        minX -= margin;
        minY -= margin;
        width += margin * 2;
        height += margin * 2;

        svgEl.setAttribute('viewBox', `${minX} ${minY} ${width} ${height}`);
        svgEl.setAttribute('width', String(width));
        svgEl.setAttribute('height', String(height));

        return { svgString: new XMLSerializer().serializeToString(svgEl), width, height };
    }

    async function generateMindMapCanvas() {
        if (!lastMindMapSvg) {
            showErrorBanner('Download කරන්න Mind Map එකක් නෑ.');
            return null;
        }

        try {
            const liveSvg = mindmapContainer.querySelector('svg');
            if (!liveSvg) {
                showErrorBanner('Mind Map එක load වී නොමැත.');
                return null;
            }
            const svgClone = liveSvg.cloneNode(true);

            svgClone.querySelectorAll('path, line, polyline').forEach(p => {
                if (p.closest('marker')) return;
                p.setAttribute('stroke', '#8b6fd6');
                p.setAttribute('fill', 'none');
                p.setAttribute('stroke-width', '2.5');
                p.style.setProperty('stroke', '#8b6fd6', 'important');
                p.style.setProperty('fill', 'none', 'important');
            });

            let width = 0, height = 0;
            const vbAttr = svgClone.getAttribute('viewBox');
            let vx = 0, vy = 0;
            if (vbAttr) {
                const parts = vbAttr.trim().split(/\s+/).map(Number);
                if (parts.length === 4) {
                    vx = parts[0]; vy = parts[1]; width = parts[2]; height = parts[3];
                }
            }
            const wAttr = svgClone.getAttribute('width');
            const hAttr = svgClone.getAttribute('height');
            if (wAttr && !wAttr.includes('%')) width = parseFloat(wAttr) || width;
            if (hAttr && !hAttr.includes('%')) height = parseFloat(hAttr) || height;
            if (!width || !height) { width = width || 1200; height = height || 800; }

            const brutePadding = 60;
            const finalWidth = width + brutePadding * 2;
            const finalHeight = height + brutePadding * 2;
            svgClone.setAttribute('viewBox', `${vx - brutePadding} ${vy - brutePadding} ${width + brutePadding * 2} ${height + brutePadding * 2}`);
            svgClone.setAttribute('width', String(finalWidth));
            svgClone.setAttribute('height', String(finalHeight));

            const bgRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            bgRect.setAttribute('x', String(vx - brutePadding));
            bgRect.setAttribute('y', String(vy - brutePadding));
            bgRect.setAttribute('width', String(width + brutePadding * 2));
            bgRect.setAttribute('height', String(height + brutePadding * 2));
            bgRect.setAttribute('fill', '#14141e');
            svgClone.insertBefore(bgRect, svgClone.firstChild);

            let svgString = new XMLSerializer().serializeToString(svgClone);
            svgString = svgString.replace(/Plus Jakarta Sans,?\s*/g, '');

            const svgBase64 = btoa(unescape(encodeURIComponent(svgString)));
            const svgDataUrl = `data:image/svg+xml;base64,${svgBase64}`;

            const img = new Image();
            await new Promise((resolve, reject) => {
                img.onload = resolve;
                img.onerror = reject;
                img.src = svgDataUrl;
            });

            const SAFE_MAX_CANVAS_DIMENSION = 4000;
            const desiredScale = 2.5;
            const largestSide = Math.max(finalWidth, finalHeight);
            const adaptiveScale = Math.min(desiredScale, SAFE_MAX_CANVAS_DIMENSION / largestSide);

            const canvas = document.createElement('canvas');
            canvas.width = Math.round(finalWidth * adaptiveScale);
            canvas.height = Math.round(finalHeight * adaptiveScale);
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

            // NEW: subtle brand watermark, bottom-right corner. When a
            // student shares this PNG/PDF with classmates (WhatsApp,
            // print, etc.), this small credit line is how a friend who
            // never used the app discovers it — organic, zero-cost
            // growth from content that's already being shared anyway.
            const watermarkFontSize = Math.max(14, Math.round(20 * adaptiveScale));
            ctx.font = `600 ${watermarkFontSize}px 'Plus Jakarta Sans', sans-serif`;
            ctx.fillStyle = 'rgba(255, 255, 255, 0.42)';
            ctx.textAlign = 'right';
            ctx.textBaseline = 'bottom';
            const watermarkMargin = Math.round(18 * adaptiveScale);
            ctx.fillText('Made with NoteWav AI · notewav.onrender.com', canvas.width - watermarkMargin, canvas.height - watermarkMargin);

            return canvas;
        } catch (err) {
            console.error('Mind map canvas render error:', err);
            showErrorBanner('Mind map render කරගැනීම අසාර්ථක විය: ' + (err && err.message ? err.message : err));
            return null;
        }
    }

    if (mindmapDownloadPdfBtn) {
        mindmapDownloadPdfBtn.addEventListener('click', async function() {
            const canvas = await generateMindMapCanvas();
            if (!canvas) return;
            trackUsageEvent('pdf_downloaded');

            try {
                const imgData = canvas.toDataURL('image/jpeg', 0.93);

                const { jsPDF } = window.jspdf;
                const orientation = canvas.width >= canvas.height ? 'l' : 'p';
                const pdf = new jsPDF({ orientation, unit: 'px', format: [canvas.width, canvas.height], compress: true });
                pdf.addImage(imgData, 'JPEG', 0, 0, canvas.width, canvas.height);

                const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);

                if (isMobile && navigator.share && navigator.canShare) {
                    const pdfBlob = pdf.output('blob');
                    const pdfFile = new File([pdfBlob], 'notewav_mindmap.pdf', { type: 'application/pdf' });

                    if (navigator.canShare({ files: [pdfFile] })) {
                        try {
                            await navigator.share({ files: [pdfFile], title: 'NoteWav Mind Map' });
                        } catch (shareErr) {
                            if (shareErr && shareErr.name !== 'AbortError') {
                                console.error('Share failed:', shareErr);
                                showErrorBanner('PDF share කරගැනීම අසාර්ථක විය: ' + shareErr.message);
                            }
                        }
                        return;
                    }
                }

                if (isMobile) {
                    const pdfBlobUrl = pdf.output('bloburl');
                    window.location.href = pdfBlobUrl;
                    showErrorBanner('PDF එක open වෙනවා — Share/Download icon එකෙන් save කරගන්න.');
                } else {
                    pdf.save('notewav_mindmap.pdf');
                }
            } catch (err) {
                console.error('PDF export error:', err);
                showErrorBanner('PDF හදන්න බැරි උනා: ' + (err && err.message ? err.message : err));
            }
        });
    }

    if (mindmapDownloadPngBtn) {
        mindmapDownloadPngBtn.addEventListener('click', async function() {
            const canvas = await generateMindMapCanvas();
            if (!canvas) return;
            trackUsageEvent('png_downloaded');

            try {
                const imgData = canvas.toDataURL('image/png');
                const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);

                if (isMobile && navigator.share && navigator.canShare) {
                    const pngBlob = await (await fetch(imgData)).blob();
                    const pngFile = new File([pngBlob], 'notewav_mindmap.png', { type: 'image/png' });

                    if (navigator.canShare({ files: [pngFile] })) {
                        try {
                            await navigator.share({ files: [pngFile], title: 'NoteWav Mind Map' });
                        } catch (shareErr) {
                            if (shareErr && shareErr.name !== 'AbortError') {
                                console.error('Share failed:', shareErr);
                                showErrorBanner('PNG share කරගැනීම අසාර්ථක විය: ' + shareErr.message);
                            }
                        }
                        return;
                    }
                }

                const link = document.createElement('a');
                link.href = imgData;
                link.download = 'notewav_mindmap.png';
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
            } catch (err) {
                console.error('PNG export error:', err);
                showErrorBanner('PNG හදන්න බැරි උනා: ' + (err && err.message ? err.message : err));
            }
        });
    }

    // ========================================
    // IMAGE UPLOAD & OCR
    // ========================================
    const uploadSourceMenu = document.getElementById('upload-source-menu');
    const uploadSourceGallery = document.getElementById('upload-source-gallery');
    const uploadSourceCamera = document.getElementById('upload-source-camera');
    const uploadSourcePdf = document.getElementById('upload-source-pdf');
    const pdfInput = document.getElementById('pdf-input');

    function closeUploadSourceMenu() {
        if (uploadSourceMenu) uploadSourceMenu.classList.add('hidden');
    }

    function toggleUploadSourceMenu() {
        if (uploadSourceMenu) uploadSourceMenu.classList.toggle('hidden');
    }

    uploadArea.addEventListener('click', function(e) {
        if (e.target.closest('.image-preview')) return;
        if (isOCRRunning) return;
        toggleUploadSourceMenu();
    });

    uploadArea.addEventListener('keydown', function(e) {
        if (e.code !== 'Enter' && e.code !== 'Space') return;
        e.preventDefault();
        if (e.target.closest('.image-preview')) return;
        if (isOCRRunning) return;
        toggleUploadSourceMenu();
    });

    if (uploadSourceGallery) {
        uploadSourceGallery.addEventListener('click', function(e) {
            e.stopPropagation();
            closeUploadSourceMenu();
            imageInput.click();
        });
    }
    if (uploadSourceCamera) {
        uploadSourceCamera.addEventListener('click', function(e) {
            e.stopPropagation();
            closeUploadSourceMenu();
            cameraInput.click();
        });
    }
    if (uploadSourcePdf && pdfInput) {
        uploadSourcePdf.addEventListener('click', function(e) {
            e.stopPropagation();
            closeUploadSourceMenu();
            pdfInput.click();
        });
    }

    document.addEventListener('click', function(e) {
        if (uploadSourceMenu && !uploadSourceMenu.classList.contains('hidden')) {
            if (!e.target.closest('.upload-area-wrapper')) {
                closeUploadSourceMenu();
            }
        }
    });

    uploadArea.addEventListener('dragover', function(e) {
        e.preventDefault();
        this.classList.add('dragover');
    });

    uploadArea.addEventListener('dragleave', function(e) {
        e.preventDefault();
        this.classList.remove('dragover');
    });

    uploadArea.addEventListener('drop', function(e) {
        e.preventDefault();
        this.classList.remove('dragover');
        closeUploadSourceMenu();
        const files = Array.from(e.dataTransfer.files || []);
        if (files.length > 0 && !isOCRRunning) {
            if (files.length > 1) handleMultipleImages(files);
            else handleImage(files[0]);
        }
    });

    // NEW: 'multiple' is now enabled on the gallery file input — if the
    // person selects several photos at once (e.g. pages of the same
    // lesson), batch-process all of them into one combined note instead
    // of only ever handling a single image.
    imageInput.addEventListener('change', function(e) {
        const files = Array.from(this.files || []);
        if (files.length > 0 && !isOCRRunning) {
            if (files.length > 1) handleMultipleImages(files);
            else handleImage(files[0]);
        }
        this.value = '';
    });

    if (cameraInput) {
        cameraInput.addEventListener('change', function(e) {
            if (this.files.length > 0 && !isOCRRunning) {
                handleImage(this.files[0]);
            }
            this.value = '';
        });
    }

    if (pdfInput) {
        pdfInput.addEventListener('change', function(e) {
            if (this.files.length > 0 && !isOCRRunning) {
                handlePdfFile(this.files[0]);
            }
            this.value = '';
        });
    }

    async function handlePdfFile(file) {
        if (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
            showOcrStatus('error', 'කරුණාකර PDF ගොනුවක් තෝරන්න.');
            return;
        }
        if (file.size > 8 * 1024 * 1024) {
            showOcrStatus('error', 'PDF ගොනුවේ ප්‍රමාණය 8MB ට වඩා වැඩියි.');
            return;
        }

        isOCRRunning = true;
        uploadArea.style.opacity = '0.5';
        uploadArea.style.pointerEvents = 'none';
        showOcrStatus('loading', `📄 "${file.name}" එකෙන් පෙළ උපුටාගනිමින්...`);

        try {
            const formData = new FormData();
            formData.append('pdf', file);
            const response = await fetch('/pdf-extract', { method: 'POST', body: formData });
            const data = await response.json();

            if (data.success && data.text) {
                if (noteInput.value.trim()) {
                    noteInput.value += '\n\n' + data.text.trim();
                } else {
                    noteInput.value = data.text.trim();
                }
                autoResizeTextarea(noteInput);
                let successMsg = `✅ Pages ${data.pages}ක PDF එකෙන් පෙළ ලබාගන්නා ලදී (${data.length} අකුරු).`;
                if (data.note) successMsg += ` ⚠️ ${data.note}`;
                showOcrStatus('success', successMsg);
                trackUsageEvent('pdf_uploaded');
            } else {
                showOcrStatus('error', data.message || 'PDF එකෙන් පෙළ ලබාගැනීම අසාර්ථක විය.');
            }
        } catch (err) {
            console.error('PDF extract error:', err);
            showOcrStatus('error', 'Network error. PDF එක process කරගැනීම අසාර්ථක විය.');
        } finally {
            isOCRRunning = false;
            uploadArea.style.opacity = '1';
            uploadArea.style.pointerEvents = 'auto';
        }
    }

    // NEW: batch-processes several photos (e.g. pages 1, 2, 3 of the
    // same lesson) sequentially through the existing single-image OCR
    // endpoint, then joins all the extracted text together with clear
    // page markers into one combined note — no backend change needed,
    // this just loops the existing /ocr call per file.
    async function handleMultipleImages(files) {
        const validFiles = files.filter(f => f.type.startsWith('image/') && f.size <= 5 * 1024 * 1024);
        if (!validFiles.length) {
            showOcrStatus('error', 'වලංගු රූප හම්බුනේ නෑ (image files, එකකට 5MB ට අඩු විය යුතුයි).');
            return;
        }

        isOCRRunning = true;
        uploadArea.style.opacity = '0.5';
        uploadArea.style.pointerEvents = 'none';

        try {
            const preview = await readFileAsDataURL(validFiles[0]);
            let previewEl = uploadArea.querySelector('.image-preview');
            if (!previewEl) {
                previewEl = document.createElement('img');
                previewEl.className = 'image-preview';
                uploadArea.prepend(previewEl);
            }
            previewEl.src = preview;
            previewEl.style.display = 'block';
            const uploadText = uploadArea.querySelector('p');
            const uploadHint = uploadArea.querySelector('.upload-hint');
            const uploadIcon = uploadArea.querySelector('i');
            if (uploadText) uploadText.style.display = 'none';
            if (uploadHint) uploadHint.style.display = 'none';
            if (uploadIcon) uploadIcon.style.display = 'none';
        } catch (e) { /* preview is a nice-to-have, not critical */ }

        const collectedTexts = [];
        let anySuccess = false;

        for (let i = 0; i < validFiles.length; i++) {
            showOcrStatus('loading', `☁️ Photo ${i + 1}/${validFiles.length} — Cloud OCR මඟින් පෙළ හඳුනා ගැනීම...`);
            try {
                const formData = new FormData();
                formData.append('image', validFiles[i]);
                const response = await fetch('/ocr', { method: 'POST', body: formData });
                const data = await response.json();
                if (data.success && data.text && data.text.trim().length >= 3) {
                    collectedTexts.push(`--- Page ${i + 1} ---\n${data.text.trim()}`);
                    anySuccess = true;
                }
            } catch (err) {
                console.error(`OCR error on photo ${i + 1}:`, err);
            }
        }

        isOCRRunning = false;
        uploadArea.style.opacity = '1';
        uploadArea.style.pointerEvents = 'auto';

        if (!anySuccess) {
            showOcrStatus('error', 'කිසිම photo එකකින් පෙළක් හඳුනාගත නොහැකි විය. පැහැදිලි රූප උත්සාහ කරන්න.');
            return;
        }

        const combinedText = collectedTexts.join('\n\n');
        if (noteInput.value.trim()) {
            noteInput.value += '\n\n' + combinedText;
        } else {
            noteInput.value = combinedText;
        }
        autoResizeTextarea(noteInput);
        showOcrStatus('success', `✅ Photos ${validFiles.length}කින් පෙළ ලබාගන්නා ලදී.`);
        trackUsageEvent('photo_batch_ocr');
    }

    async function handleImage(file) {
        if (!file.type.startsWith('image/')) {
            showOcrStatus('error', 'කරුණාකර රූප ගොනුවක් තෝරන්න.');
            return;
        }

        if (file.size > 5 * 1024 * 1024) {
            showOcrStatus('error', 'රූපයේ ප්‍රමාණය 5MB ට වඩා වැඩියි.');
            return;
        }

        isOCRRunning = true;
        uploadArea.style.opacity = '0.5';
        uploadArea.style.pointerEvents = 'none';

        try {
            const imageUrl = await readFileAsDataURL(file);
            let preview = uploadArea.querySelector('.image-preview');
            if (!preview) {
                preview = document.createElement('img');
                preview.className = 'image-preview';
                uploadArea.prepend(preview);
            }
            preview.src = imageUrl;
            preview.style.display = 'block';

            const uploadText = uploadArea.querySelector('p');
            const uploadHint = uploadArea.querySelector('.upload-hint');
            const uploadIcon = uploadArea.querySelector('i');
            if (uploadText) uploadText.style.display = 'none';
            if (uploadHint) uploadHint.style.display = 'none';
            if (uploadIcon) uploadIcon.style.display = 'none';

            showOcrStatus('loading', '☁️ Cloud OCR මඟින් සිංහල පෙළ හඳුනා ගැනීම...');

            const formData = new FormData();
            formData.append('image', file);

            const response = await fetch('/ocr', { method: 'POST', body: formData });
            const data = await response.json();

            if (data.success && data.text) {
                const extractedText = data.text.trim();

                if (extractedText.length < 3) {
                    showOcrStatus('error', 'පෙළ හඳුනා ගැනීම අසාර්ථක විය. පැහැදිලි රූපයක් උත්සාහ කරන්න.');
                    return;
                }

                let langMsg = data.detected_language ? ` (${data.detected_language})` : '';

                if (noteInput.value.trim()) {
                    noteInput.value += '\n\n' + extractedText;
                } else {
                    noteInput.value = extractedText;
                }

                if (noteInput.value.length > MAX_TEXT_LENGTH) {
                    noteInput.value = noteInput.value.slice(0, MAX_TEXT_LENGTH);
                    showErrorBanner(`සටහන අකුරු ${MAX_TEXT_LENGTH}ට කප්පාදු කළා (උපරිම සීමාව).`);
                }

                noteInput.dispatchEvent(new Event('input'));
                autoResizeTextarea(noteInput);

                showOcrStatus('success', `✅ පෙළ සාර්ථකව උපුටා ගන්නා ලදී! අක්ෂර ${extractedText.length} කි${langMsg}`);
                noteInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
            } else {
                showOcrStatus('error', data.message || 'පෙළ හඳුනා ගැනීම අසාර්ථක විය.');
            }

        } catch (error) {
            console.error('OCR Error:', error);
            showOcrStatus('error', '❌ Cloud OCR අසාර්ථක විය.');
        } finally {
            isOCRRunning = false;
            uploadArea.style.opacity = '1';
            uploadArea.style.pointerEvents = 'auto';
        }
    }

    function showOcrStatus(type, message) {
        ocrStatus.classList.remove('hidden');
        const iconMap = {
            'loading': '<span class="mini-wave"><span></span><span></span><span></span><span></span></span>',
            'success': '<i class="fas fa-check-circle" style="color: #22c55e;"></i>',
            'error': '<i class="fas fa-exclamation-circle" style="color: #ef4444;"></i>'
        };
        ocrStatus.innerHTML = `${iconMap[type] || iconMap.loading} <span>${message}</span>`;

        if (type === 'success' || type === 'error') {
            setTimeout(() => {
                ocrStatus.classList.add('hidden');
            }, 8000);
        }
    }

    function readFileAsDataURL(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = (e) => resolve(e.target.result);
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    }

    // ========================================
    // PROCESS NOTE (Feature 1: podcast script + mind map)
    // ========================================
    processBtn.addEventListener('click', async function() {
        const text = noteInput.value.trim();
        if (!text) {
            showErrorBanner('කරුණාකර පාඩම් සටහනක් ඇතුළත් කරන්න.');
            return;
        }

        if (text.length > MAX_TEXT_LENGTH) {
            showErrorBanner(`සටහන ඉතා දිගයි — අකුරු ${MAX_TEXT_LENGTH}ක සීමාවක් තිබේ (දැනට අකුරු ${text.length}ක් ඇත).`);
            return;
        }

        const mode = document.querySelector('input[name="study_mode"]:checked').value;

        processBtn.innerHTML = '<span class="mini-wave"><span></span><span></span><span></span><span></span></span> Processing...';
        processBtn.disabled = true;

        try {
            const response = await fetch('/process-note', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, mode, output_language: outputLanguage }),
            });

            const data = await response.json();

            if (data.status === 'success') {
                scriptOutput.value = data.processed_text;
                safetySection.classList.remove('hidden');
                audioSection.classList.add('hidden');
                trackUsageEvent('note_processed');

                if (data.ai_processed === false && data.warning) {
                    // FIX: this is a "soft failure" — Gemini processing
                    // didn't fully work and a warning banner (looks
                    // like an error) is shown to the person, so it
                    // shouldn't count as a completed note toward
                    // leveling up.
                    showErrorBanner(data.warning);
                } else {
                    incrementNotesProcessedCount();
                }

                await renderMindMap(data.mermaid_code_si, data.mermaid_code_en);

                if (audio) {
                    audio.pause();
                    audio = null;
                    isPlaying = false;
                    playPauseBtn.innerHTML = '<i class="fas fa-play"></i>';
                    updateProgress(0);
                    currentTimeEl.textContent = '0:00';
                    totalTimeEl.textContent = '0:00';
                    document.querySelector('.player-container').classList.remove('playing');
                    stopWaveformAnimation();
                    highlightUnits = [];
                }

                safetySection.scrollIntoView({ behavior: 'smooth', block: 'center' });
            } else {
                showErrorBanner(data.message || 'මොකක් හරි ගැටලුවක් ආවා.');
            }
        } catch (error) {
            console.error('Error:', error);
            showErrorBanner('Network error. කරුණාකර නැවත උත්සාහ කරන්න.');
        } finally {
            processBtn.innerHTML = '<i class="fas fa-magic"></i> Script එක සකසන්න';
            processBtn.disabled = false;
        }
    });

    // ========================================
    // GENERATE AUDIO
    // ========================================
    generateAudioBtn.addEventListener('click', async function() {
        const text = scriptOutput.value.trim();
        if (!text) {
            showErrorBanner('Audio එකට හරවන්න text එකක් නැහැ. කරුණාකර පළමුව සටහන process කරන්න.');
            return;
        }

        if (text.length > MAX_TEXT_LENGTH) {
            showErrorBanner(`Script එක ඉතා දිගයි — අකුරු ${MAX_TEXT_LENGTH}ක සීමාවක් තිබේ.`);
            return;
        }

        // NEW: Gemini (Natural AI) is a paid feature after the first 3
        // free trials — check BEFORE spending any time/API calls
        // generating audio the person can't actually afford/use.
        let estimatedCoinCost = 0;
        if (ttsEngine === 'gemini') {
            const freeUsesLeft = getGeminiFreeTrialsLeft();
            if (freeUsesLeft <= 0) {
                estimatedCoinCost = estimateGeminiCoinCost(text.length, ttsModelVersion);
                if (getCoinsBalance() < estimatedCoinCost) {
                    showErrorBanner('Natural (AI) Voice එකට ප්‍රමාණවත් coins නෑ. Coins මිලදී ගන්න පුළුවන් වෙන feature එකක් ඉක්මනින් එනවා!');
                    return;
                }
            }
        }

        generateAudioBtn.innerHTML = ttsEngine === 'gemini'
            ? '<span class="mini-wave"><span></span><span></span><span></span><span></span></span> AI Voice Generating (ටිකක් වේලා ගන්නවා)...'
            : '<span class="mini-wave"><span></span><span></span><span></span><span></span></span> Generating...';
        generateAudioBtn.disabled = true;

        try {
            const response = await fetch('/tts', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    text,
                    engine: ttsEngine,
                    voice_name: (ttsEngine === 'gemini' && ttsVoiceSelect) ? ttsVoiceSelect.value : undefined,
                    model_version: ttsEngine === 'gemini' ? ttsModelVersion : undefined,
                }),
            });

            const data = await response.json();

            if (data.status === 'success') {
                audioSection.classList.remove('hidden');
                renderLyrics(text, data.sentence_timings);
                trackUsageEvent('audio_generated');
                saveOfflineAudioEntry(text, data.audio_url);

                // NEW: only charge AFTER a successful generation — a
                // failed/errored request shouldn't cost the person
                // anything.
                if (data.engine === 'gemini') {
                    const freeUsesLeft = getGeminiFreeTrialsLeft();
                    if (freeUsesLeft > 0) {
                        useGeminiFreeTrial();
                    } else {
                        const coinCost = data.coin_cost || estimateGeminiCoinCost(text.length, ttsModelVersion);
                        spendCoins(coinCost);
                    }
                    if (typeof updateGeminiTrialStatus === 'function') updateGeminiTrialStatus();
                }

                if (audio) {
                    audio.pause();
                    audio = null;
                }

                audio = new Audio(data.audio_url);
                currentAudioEngine = data.engine || 'gtts';
                audio.playbackRate = getEffectivePlaybackRate();
                audio.volume = playbackVolume;
                setupAudioAnalyser(audio);

                audio.addEventListener('loadedmetadata', function() {
                    const duration = audio.duration;
                    totalTimeEl.textContent = (!isNaN(duration) && isFinite(duration) && duration > 0)
                        ? formatTime(duration) : '0:00';
                });

                audio.addEventListener('timeupdate', function() {
                    const current = audio.currentTime;
                    const duration = audio.duration;

                    if (!isNaN(current) && !isNaN(duration) && isFinite(duration) && duration > 0) {
                        currentTimeEl.textContent = formatTime(current);
                        updateProgress((current / duration) * 100);
                        updateHighlight(current);
                    }
                    trackStudyTime(current);
                });

                audio.addEventListener('ended', function() {
                    isPlaying = false;
                    playPauseBtn.innerHTML = '<i class="fas fa-play"></i>';
                    updateProgress(0);
                    currentTimeEl.textContent = '0:00';
                    document.querySelector('.player-container').classList.remove('playing');
                    stopWaveformAnimation();
                    document.querySelectorAll('.lyric-line').forEach(el => el.classList.remove('active'));
                });

                audio.addEventListener('error', function(e) {
                    console.error('Audio Error:', e);
                    showErrorBanner('Audio playback error. Please try again.');
                });

                // FIX (couldn't seek on mobile): the old handler only
                // listened for 'click', which fires on a simple tap but
                // never fires while dragging a finger along the bar —
                // so seeking only sort-of worked with an exact single
                // tap and never with a drag-to-scrub gesture. Pointer
                // Events unify mouse and touch, so this now handles
                // both a tap AND a drag consistently on any device.
                let isDraggingProgress = false;

                function seekFromPointerEvent(e) {
                    if (!audio || !audio.duration || isNaN(audio.duration) || !isFinite(audio.duration)) return;
                    const rect = progressBar.getBoundingClientRect();
                    const x = e.clientX - rect.left;
                    const percentage = Math.min(1, Math.max(0, x / rect.width));
                    const seekTime = percentage * audio.duration;

                    audio.currentTime = seekTime;
                    currentTimeEl.textContent = formatTime(seekTime);
                    updateProgress(percentage * 100);
                    updateHighlight(seekTime);
                }

                progressBar.addEventListener('pointerdown', function(e) {
                    isDraggingProgress = true;
                    try { progressBar.setPointerCapture(e.pointerId); } catch (err) { /* older browsers — safe to ignore */ }
                    seekFromPointerEvent(e);
                });
                progressBar.addEventListener('pointermove', function(e) {
                    if (!isDraggingProgress) return;
                    seekFromPointerEvent(e);
                });
                progressBar.addEventListener('pointerup', function() {
                    isDraggingProgress = false;
                });
                progressBar.addEventListener('pointercancel', function() {
                    isDraggingProgress = false;
                });

                audio.play()
                    .then(() => {
                        isPlaying = true;
                        playPauseBtn.innerHTML = '<i class="fas fa-pause"></i>';
                        document.querySelector('.player-container').classList.add('playing');
                        if (audioCtx && audioCtx.state === 'suspended') {
                            audioCtx.resume();
                        }
                        startWaveformAnimation();
                    })
                    .catch((error) => {
                        console.error('Auto-play error:', error);
                        isPlaying = false;
                        playPauseBtn.innerHTML = '<i class="fas fa-play"></i>';
                    });

                audioSection.scrollIntoView({ behavior: 'smooth', block: 'center' });
            } else {
                showErrorBanner(data.message || 'Audio generate කිරීම අසාර්ථක විය.');
            }
        } catch (error) {
            console.error('Error:', error);
            showErrorBanner('Network error. කරුණාකර නැවත උත්සාහ කරන්න.');
        } finally {
            generateAudioBtn.innerHTML = '<i class="fas fa-microphone"></i> තරංග උත්පාදනය කරන්න';
            generateAudioBtn.disabled = false;
        }
    });

    // ========================================
    // PLAY / PAUSE
    // ========================================
    playPauseBtn.addEventListener('click', function() {
        if (!audio) {
            showErrorBanner('Audio එකක් නැහැ. කරුණාකර පළමුව audio generate කරන්න.');
            return;
        }

        if (isPlaying) {
            audio.pause();
            isPlaying = false;
            playPauseBtn.innerHTML = '<i class="fas fa-play"></i>';
            document.querySelector('.player-container').classList.remove('playing');
            stopWaveformAnimation();
        } else {
            audio.play()
                .then(() => {
                    isPlaying = true;
                    playPauseBtn.innerHTML = '<i class="fas fa-pause"></i>';
                    document.querySelector('.player-container').classList.add('playing');
                    if (audioCtx && audioCtx.state === 'suspended') {
                        audioCtx.resume();
                    }
                    startWaveformAnimation();
                })
                .catch((error) => {
                    console.error('Play error:', error);
                    showErrorBanner('Error playing audio. Please try again.');
                });
        }
    });

    // ========================================
    // STOP
    // ========================================
    stopBtn.addEventListener('click', function() {
        if (audio) {
            audio.pause();
            audio.currentTime = 0;
            isPlaying = false;
            playPauseBtn.innerHTML = '<i class="fas fa-play"></i>';
            updateProgress(0);
            currentTimeEl.textContent = '0:00';
            document.querySelector('.player-container').classList.remove('playing');
            stopWaveformAnimation();
            document.querySelectorAll('.lyric-line').forEach(el => el.classList.remove('active'));
        }
    });

    // ========================================
    // SKIP FORWARD / BACKWARD (10s) — podcast-style navigation
    // ========================================
    if (skipBackBtn) {
        skipBackBtn.addEventListener('click', function() {
            if (audio) audio.currentTime = Math.max(0, audio.currentTime - 10);
        });
    }
    if (skipForwardBtn) {
        skipForwardBtn.addEventListener('click', function() {
            if (audio) audio.currentTime = Math.min(audio.duration || Infinity, audio.currentTime + 10);
        });
    }

    // ========================================
    // DOWNLOAD
    // ========================================
    downloadBtn.addEventListener('click', function() {
        if (!audio) {
            showErrorBanner('Download කරන්න audio එකක් නැහැ.');
            return;
        }

        const link = document.createElement('a');
        link.href = audio.src;
        link.download = 'notewav_audio.mp3';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    });

    // ========================================
    // RESET
    // ========================================
    resetBtn.addEventListener('click', function() {
        if (audio) {
            audio.pause();
            audio = null;
            isPlaying = false;
            playPauseBtn.innerHTML = '<i class="fas fa-play"></i>';
            updateProgress(0);
            currentTimeEl.textContent = '0:00';
            totalTimeEl.textContent = '0:00';
            document.querySelector('.player-container').classList.remove('playing');
            stopWaveformAnimation();
            highlightUnits = [];
        }

        safetySection.classList.add('hidden');
        audioSection.classList.add('hidden');
        if (mindmapSection) mindmapSection.classList.add('hidden');
        const quizSectionResetEl = document.getElementById('quiz-section');
        if (quizSectionResetEl) quizSectionResetEl.classList.add('hidden');
        const quizBodyResetEl = document.getElementById('quiz-body');
        if (quizBodyResetEl) quizBodyResetEl.innerHTML = '';
        if (mindmapContainer) mindmapContainer.innerHTML = '<p class="mindmap-empty">Mind map එකක් තවම නෑ.</p>';
        lastMindMapSvg = '';
        mermaidCodes = { si: '', en: '' };
        mindmapSvgCache = { si: '', en: '' };
        currentMindMapLang = 'si';
        setMindMapLangButtonsUI('si');
        closeMindMapModal();

        scriptOutput.value = '';
        noteInput.value = '';
        charCount.textContent = '0';
        if (characterCountEl) characterCountEl.classList.remove('over-limit');
        noteInput.focus();
        highlightContainer.innerHTML = '';

        const preview = uploadArea.querySelector('.image-preview');
        if (preview) preview.remove();

        const uploadText = uploadArea.querySelector('p');
        const uploadHint = uploadArea.querySelector('.upload-hint');
        const uploadIcon = uploadArea.querySelector('i');
        if (uploadText) uploadText.style.display = 'block';
        if (uploadHint) uploadHint.style.display = 'block';
        if (uploadIcon) uploadIcon.style.display = 'block';
        ocrStatus.classList.add('hidden');

        window.scrollTo({ top: 0, behavior: 'smooth' });
    });

    // ========================================
    // KEYBOARD SHORTCUTS
    // ========================================
    document.addEventListener('keydown', function(e) {
        if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA') {
            if (e.code === 'Space') {
                e.preventDefault();
                if (!audioSection.classList.contains('hidden')) {
                    playPauseBtn.click();
                }
            }
        }

        if (e.code === 'Escape') {
            if (!audioSection.classList.contains('hidden')) {
                stopBtn.click();
            }
        }
    });

    // ========================================
    // AUTO-RESIZE TEXTAREA
    // ========================================
    document.querySelectorAll('textarea').forEach(textarea => {
        textarea.addEventListener('input', function() {
            autoResizeTextarea(this);
        });
    });

    // ========================================
    // QUIZ GENERATION (active-recall study tool)
    // ========================================
    const generateQuizBtn = document.getElementById('generate-quiz-btn');
    const quizBody = document.getElementById('quiz-body');

    function escapeHtmlQuiz(str) {
        const div = document.createElement('div');
        div.textContent = str == null ? '' : String(str);
        return div.innerHTML;
    }

    function renderQuiz(questions) {
        let html = '<form id="quiz-form">';
        questions.forEach((q, qIndex) => {
            html += `<div class="quiz-question" style="margin-bottom: 18px; padding: 14px; background: rgba(255,255,255,0.03); border-radius: 12px; border: 1px solid var(--border-color);">`;
            html += `<p style="font-weight: 600; margin-bottom: 10px;">${qIndex + 1}. ${escapeHtmlQuiz(q.question)}</p>`;
            (q.options || []).forEach((opt, oIndex) => {
                html += `<label style="display: flex; align-items: center; gap: 8px; padding: 6px 0; cursor: pointer; font-size: 0.9rem;">
                    <input type="radio" name="q${qIndex}" value="${oIndex}" style="accent-color: #6b30ff;">
                    <span>${escapeHtmlQuiz(opt)}</span>
                </label>`;
            });
            html += `<div class="quiz-answer-feedback" id="quiz-feedback-${qIndex}" style="margin-top: 8px; font-size: 0.85rem; display: none;"></div>`;
            html += `</div>`;
        });
        html += `<button type="submit" class="btn-primary" style="margin-top: 8px;"><i class="fas fa-check"></i> Answers check කරන්න</button>`;
        html += `<p id="quiz-score" style="margin-top: 12px; font-weight: 700; display: none;"></p>`;
        html += `</form>`;
        quizBody.innerHTML = html;

        document.getElementById('quiz-form').addEventListener('submit', function(e) {
            e.preventDefault();
            let correctCount = 0;
            questions.forEach((q, qIndex) => {
                const selected = document.querySelector(`input[name="q${qIndex}"]:checked`);
                const feedbackEl = document.getElementById(`quiz-feedback-${qIndex}`);
                feedbackEl.style.display = 'block';
                if (!selected) {
                    feedbackEl.innerHTML = '<span style="color:#f59e0b;">⚠️ Answer එකක් තෝරලා නෑ</span>';
                    return;
                }
                const selectedIndex = parseInt(selected.value, 10);
                if (selectedIndex === q.correct_index) {
                    correctCount++;
                    feedbackEl.innerHTML = '<span style="color:#22c55e;">✅ හරි!</span>';
                } else {
                    const correctText = (q.options || [])[q.correct_index] || '';
                    feedbackEl.innerHTML = `<span style="color:#ef4444;">❌ වැරදියි — හරි answer එක: ${escapeHtmlQuiz(correctText)}</span>`;
                }
            });
            const scoreEl = document.getElementById('quiz-score');
            scoreEl.style.display = 'block';
            scoreEl.textContent = `ලකුණු: ${correctCount} / ${questions.length}`;
        });
    }

    if (generateQuizBtn && quizBody) {
        generateQuizBtn.addEventListener('click', async function() {
            const sourceText = (scriptOutput && scriptOutput.value) || (noteInput && noteInput.value) || '';
            const text = sourceText.trim();
            if (!text) {
                showErrorBanner('Quiz එකක් හදන්න content එකක් නෑ — කලින් note එකක් process කරන්න.');
                return;
            }

            generateQuizBtn.innerHTML = '<span class="mini-wave"><span></span><span></span><span></span><span></span></span> Quiz හදමින්...';
            generateQuizBtn.disabled = true;
            quizBody.innerHTML = '';

            try {
                const response = await fetch('/generate-quiz', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text }),
                });
                const data = await response.json();

                if (data.status === 'success' && Array.isArray(data.questions) && data.questions.length) {
                    renderQuiz(data.questions);
                } else {
                    showErrorBanner(data.message || 'Quiz එක හදාගැනීම අසාර්ථක විය.');
                }
            } catch (err) {
                console.error('Quiz generation error:', err);
                showErrorBanner('Network error. Quiz එක හදාගැනීම අසාර්ථක විය.');
            } finally {
                generateQuizBtn.innerHTML = '<i class="fas fa-list-check"></i> Quiz එකක් හදන්න';
                generateQuizBtn.disabled = false;
            }
        });
    }

    console.log('🎵 NoteWav AI Loaded — Mind Maps + gTTS narration ready!');
});

// ========================================
// SPLASH SCREEN: guaranteed hide (JS fallback)
// ========================================
window.addEventListener('load', function() {
    setTimeout(function() {
        const splash = document.getElementById('splash-screen');
        if (splash) {
            splash.style.transition = 'opacity 0.4s ease';
            splash.style.opacity = '0';
            setTimeout(() => { splash.style.display = 'none'; }, 400);
        }
    }, 3300);
});

// ========================================
// PWA: SERVICE WORKER REGISTRATION
// ========================================
// FIX (install button never appeared, especially on Samsung Internet):
// previously this registered '/static/sw.js', which auto-restricts the
// service worker's scope to '/static/' — meaning it could never
// control the actual home page ('/'). Several browsers require the
// service worker to control start_url before offering the install
// prompt, so that check was silently failing. Now the SAME sw.js file
// is served from the app's root path via a new Flask route
// (@app.route('/sw.js') in app.py, with a Service-Worker-Allowed: /
// header for extra safety), and registered here with an explicit
// scope of '/' so it can control the entire site as the manifest
// intends.
// NEW: a mobile-device check shared by both install-button code paths
// below. The person only wants the "Install app" button to ever show
// up on phones/tablets — desktop browsers (even ones that technically
// support PWA install, like desktop Chrome/Edge) should never show it.
function isMobileDeviceForInstall() {
    return /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
}

if ('serviceWorker' in navigator) {
    window.addEventListener('load', function() {
        // FIX (Samsung Internet still not showing install prompt after
        // the scope fix): a PREVIOUS visit may have already registered
        // the old, wrongly-scoped worker at '/static/sw.js' before this
        // fix was deployed. That stale registration doesn't just go
        // away on its own — the browser can end up tracking two
        // registrations for the same site. Explicitly finding and
        // unregistering any worker whose scope still contains
        // '/static/' clears that leftover confusion before registering
        // the correctly-scoped one at '/'.
        navigator.serviceWorker.getRegistrations().then((registrations) => {
            registrations.forEach((reg) => {
                if (reg.scope && reg.scope.includes('/static/')) {
                    console.log('🧹 Unregistering stale /static/-scoped service worker:', reg.scope);
                    reg.unregister();
                }
            });
        }).catch(() => { /* non-critical, ignore */ });

        navigator.serviceWorker.register('/sw.js', { scope: '/' })
            .then((reg) => console.log('✅ Service worker registered:', reg.scope))
            .catch((err) => console.warn('⚠️ Service worker registration failed:', err));
    });
}

// ========================================
// PWA: CUSTOM "INSTALL APP" BUTTON
// ========================================
let deferredInstallPrompt = null;

window.addEventListener('beforeinstallprompt', function(e) {
    e.preventDefault();
    deferredInstallPrompt = e;
    // NEW: only ever surface the button on mobile, per request — even
    // though Chromium desktop browsers CAN fire this event and support
    // installing, the person doesn't want the button appearing there.
    if (!isMobileDeviceForInstall()) return;
    const btn = document.getElementById('install-app-btn');
    if (btn) btn.classList.remove('hidden');
});

document.addEventListener('DOMContentLoaded', function() {
    const installBtn = document.getElementById('install-app-btn');
    if (!installBtn) return;

    installBtn.addEventListener('click', async function() {
        if (!deferredInstallPrompt) return;
        deferredInstallPrompt.prompt();
        const { outcome } = await deferredInstallPrompt.userChoice;
        console.log('Install prompt outcome:', outcome);
        deferredInstallPrompt = null;
        installBtn.classList.add('hidden');
    });
});

window.addEventListener('appinstalled', function() {
    const btn = document.getElementById('install-app-btn');
    if (btn) btn.classList.add('hidden');
    deferredInstallPrompt = null;
    console.log('🎉 NoteWav AI installed as an app!');
});

// ========================================
// FIREFOX / OTHER NON-CHROMIUM: manual "Add to Home Screen" fallback
// ========================================
// NEW: Firefox (desktop & Android) never fires 'beforeinstallprompt'
// at all — that's a Chromium-only API (Chrome, Samsung Internet,
// Edge). Without this, the install button would just silently never
// appear there. This detects that case and shows the SAME button, but
// clicking it opens simple manual instructions instead of the native
// prompt.
document.addEventListener('DOMContentLoaded', function() {
    const isFirefox = navigator.userAgent.toLowerCase().includes('firefox');
    const isStandalone = window.matchMedia('(display-mode: standalone)').matches;
    const btn = document.getElementById('install-app-btn');
    // NEW: mobile-only, same as the Chromium install button above —
    // this fallback used to show on desktop Firefox too.
    if (!btn || !isFirefox || isStandalone || !isMobileDeviceForInstall()) return;

    setTimeout(() => {
        btn.classList.remove('hidden');
    }, 2500);

    btn.addEventListener('click', function firefoxInstallHandler(e) {
        if (deferredInstallPrompt) return; // let the normal Chromium flow handle it if it exists
        e.stopImmediatePropagation();
        alert('Firefox එකේ Install කරන්න:\n\n☰ Menu (⋮) → "Install" හෝ "Add to Home screen" click කරන්න.');
    }, true);
});

// ========================================
// SAFETY-CHECK TEXT FONT SIZE ADJUSTMENT
// ========================================
document.addEventListener('DOMContentLoaded', function() {
    const FONT_SIZE_KEY = 'notewav_script_font_size';
    const MIN_PERCENT = 70;
    const MAX_PERCENT = 160;
    const STEP = 10;
    const BASE_PX = 16;

    const decreaseBtn = document.getElementById('font-size-decrease');
    const increaseBtn = document.getElementById('font-size-increase');
    const valueEl = document.getElementById('font-size-value');
    const targetTextarea = document.getElementById('script-output');
    if (!decreaseBtn || !increaseBtn || !valueEl || !targetTextarea) return;

    function applyFontSize(percent) {
        targetTextarea.style.fontSize = (BASE_PX * percent / 100) + 'px';
        valueEl.textContent = percent + '%';
        try {
            localStorage.setItem(FONT_SIZE_KEY, String(percent));
        } catch (e) {
            console.warn('Could not save font size preference:', e);
        }
    }

    let currentPercent = 100;
    try {
        const saved = parseInt(localStorage.getItem(FONT_SIZE_KEY), 10);
        if (!isNaN(saved) && saved >= MIN_PERCENT && saved <= MAX_PERCENT) {
            currentPercent = saved;
        }
    } catch (e) {
        // ignore, fall back to default
    }
    applyFontSize(currentPercent);

    decreaseBtn.addEventListener('click', function() {
        currentPercent = Math.max(MIN_PERCENT, currentPercent - STEP);
        applyFontSize(currentPercent);
    });
    increaseBtn.addEventListener('click', function() {
        currentPercent = Math.min(MAX_PERCENT, currentPercent + STEP);
        applyFontSize(currentPercent);
    });
});

// ========================================
// STUDY STREAK TRACKER
// ========================================
document.addEventListener('DOMContentLoaded', function() {
    const STREAK_KEY = 'notewav_streak_data';
    const streakCountEl = document.getElementById('streak-count-drawer');
    if (!streakCountEl) return;

    function todayLocalString() {
        const now = new Date();
        const y = now.getFullYear();
        const m = String(now.getMonth() + 1).padStart(2, '0');
        const d = String(now.getDate()).padStart(2, '0');
        return `${y}-${m}-${d}`;
    }

    function daysBetween(dateStrA, dateStrB) {
        const a = new Date(dateStrA + 'T00:00:00');
        const b = new Date(dateStrB + 'T00:00:00');
        return Math.round((b - a) / (1000 * 60 * 60 * 24));
    }

    try {
        const today = todayLocalString();
        let data = null;
        try {
            data = JSON.parse(localStorage.getItem(STREAK_KEY));
        } catch (e) {
            data = null;
        }

        let streak;
        if (!data || !data.lastDate) {
            streak = 1;
        } else if (data.lastDate === today) {
            streak = data.streak || 1;
        } else {
            const gap = daysBetween(data.lastDate, today);
            streak = gap === 1 ? (data.streak || 0) + 1 : 1;
        }

        localStorage.setItem(STREAK_KEY, JSON.stringify({ lastDate: today, streak }));
        streakCountEl.textContent = `${streak} days`;
        syncAccountToServer({ streak: streak, last_streak_date: today });
    } catch (e) {
        console.warn('Study streak tracking unavailable:', e);
    }
});

// ========================================
// APP UI LANGUAGE (Sinhala / English toggle)
// ========================================
const NOTEWAV_TRANSLATIONS = {
    my_library: { si: 'My Library', en: 'My Library' },
    settings_title: { si: 'සැකසුම්', en: 'Settings' },
    menu_title: { si: 'මෙනුව (Menu)', en: 'Menu' },
    settings_language_label: { si: 'App භාෂාව (Language)', en: 'App Language' },
    library_search_placeholder: { si: 'Title/Subject/Content එකෙන් හොයන්න...', en: 'Search by Title/Subject/Content...' },
    combine_toggle_btn: { si: 'එකතු කරන්න', en: 'Combine' },
    combine_hint: {
        si: '🔗 Notes කිහිපයක් (checkbox වලින්) තෝරගන්න — ඒ සියල්ල එකට එකතු කරලා, එකම දිගු Audio episode එකක් හදාගන්න පුළුවන් (Exam කලින් Full Revision විදිහට අහන්න හොඳයි).',
        en: '🔗 Select two or more notes (checkboxes) to combine them into one long audio episode — great for a full revision listen before exams.',
    },
    header_tagline: { si: 'Smart විදිහට පාඩම් අහන්න. NoteWav AI', en: 'Listen to your notes, the smart way. NoteWav AI' },
    card1_title: { si: 'ඔබේ සටහන ඇතුළත් කරන්න', en: 'Enter Your Note' },
    ocr_section_title: { si: 'රූපයක් මඟින් කරුණු ලබාදෙන්න (OCR):', en: 'Provide content via an image (OCR):' },
    upload_area_text: { si: 'පාඩමේ රූපයක් මෙතනට ඇද දමන්න හෝ ක්ලික් කරන්න', en: 'Drag a photo of the lesson here or click to browse' },
    upload_hint: { si: 'PNG, JPG, JPEG, WEBP ගොනු පමණයි (Max 5MB)', en: 'PNG, JPG, JPEG, WEBP files only (Max 5MB)' },
    ocr_status_text: { si: 'පෙළ උපුටා ගැනීම සිදුවෙමින්...', en: 'Extracting text...' },
    note_input_placeholder: { si: 'මෙතනට ඔබේ සටහන් ඇතුළත් කරන්න. නැතිනම් කැමති ප්‍රශ්නයක් අහන්න...', en: 'Type your notes here, or ask any question you like...' },
    char_count_suffix: { si: '/ 2000 අක්ෂර', en: '/ 2000 characters' },
    study_mode_title: { si: 'අධ්‍යාපනික මාදිලිය (Study Mode):', en: 'Study Mode:' },
    output_lang_title: { si: 'Output භාෂාව (Podcast Script/Audio):', en: 'Output Language (Podcast Script/Audio):' },
    output_lang_hint: { si: 'Smart Study mode එකේදී විතරයි අදාළ වන්නේ — ඔබ ලියන භාෂාව කුමක් වුවත්, script/audio එක තෝරගත් භාෂාවෙන්ම එයි.', en: 'Only applies to Smart Study mode — whatever language you type in, the script/audio will come out in your chosen language.' },
    mode_full_desc: { si: 'ඔබේ text එකම audio + mind map', en: 'Your text becomes audio + mind map' },
    mode_smart_desc: { si: 'Podcast script + Mind Map දෙකම AI කරයි', en: 'AI creates both a podcast script + Mind Map' },
    process_btn: { si: 'Script එක සකසන්න', en: 'Prepare Script' },
    safety_title: { si: 'ආරක්ෂිත පරීක්ෂාව', en: 'Safety Check' },
    safety_info_text: { si: 'වැදගත් කරුණු මගහැරී ඇත්නම් ඔබට මෙය වෙනස් (Edit) කළ හැක:', en: 'You can Edit this if important points are missing:' },
    font_size_label: { si: 'අකුරු ප්‍රමාණය:', en: 'Font Size:' },
    subject_placeholder: { si: 'Subject (උදා: Biology, Physics)', en: 'Subject (e.g: Biology, Physics)' },
    save_to_library_btn: { si: 'Library එකට Save කරන්න', en: 'Save to Library' },
    generate_audio_btn: { si: 'තරංග උත්පාදනය කරන්න', en: 'Generate Audio' },
    reset_btn: { si: 'නැවත උත්සාහ කරන්න', en: 'Try Again' },
    mindmap_empty: { si: 'Mind map එකක් තවම නෑ.', en: 'No mind map yet.' },
    download_png_btn: { si: 'PNG එකක් විදිහට Download කරන්න', en: 'Download as PNG' },
    download_pdf_btn: { si: 'PDF එකක් විදිහට Download කරන්න', en: 'Download as PDF' },
    audio_player_title: { si: 'NoteWav වාදකය', en: 'NoteWav Player' },
    speed_label: { si: 'Speed:', en: 'Speed:' },
    volume_label: { si: 'Volume:', en: 'Volume:' },
    download_audio_btn: { si: 'Audio Download කරන්න', en: 'Download Audio' },
    share_btn: { si: 'Share කරන්න', en: 'Share' },
    share_with_audio_btn: { si: 'Audio එකත් සමඟ Share කරන්න', en: 'Share with Audio' },
    share_whatsapp_btn: { si: 'WhatsApp (Text විතරයි)', en: 'WhatsApp (Text only)' },
    share_telegram_btn: { si: 'Telegram (Text විතරයි)', en: 'Telegram (Text only)' },
    lyrics_placeholder: { si: 'Audio එක Play කරනකොට මෙතන text එක highlight වෙනවා...', en: 'Text will highlight here as the Audio plays...' },
};

// ========================================
// GEMINI (NATURAL AI) VOICE — FREE TRIALS + COIN PRICING
// ========================================
const GEMINI_FREE_TRIALS_TOTAL = 3;
const GEMINI_TRIALS_USED_KEY = 'notewav_gemini_trials_used';

function getGeminiTrialsUsed() {
    try {
        return parseInt(localStorage.getItem(GEMINI_TRIALS_USED_KEY) || '0', 10) || 0;
    } catch (e) {
        return 0;
    }
}

function getGeminiFreeTrialsLeft() {
    return Math.max(0, GEMINI_FREE_TRIALS_TOTAL - getGeminiTrialsUsed());
}

function useGeminiFreeTrial() {
    try {
        localStorage.setItem(GEMINI_TRIALS_USED_KEY, String(getGeminiTrialsUsed() + 1));
    } catch (e) {
        console.warn('Could not save Gemini trial usage:', e);
    }
}

// Mirrors the backend's calculate_gemini_tts_coin_cost() tiers exactly
// — used here only to check affordability BEFORE sending the request;
// the actual authoritative charge always comes from the server's own
// `coin_cost` in the /tts response. v31 costs exactly 2x v25, matching
// its 2x per-token API price.
function estimateGeminiCoinCost(textLength, modelVersion) {
    let base;
    if (textLength <= 500) base = 5;
    else if (textLength <= 1200) base = 12;
    else base = 20;
    return modelVersion === 'v31' ? base * 2 : base;
}

// ========================================
// LEVEL SYSTEM (gamification — notes processed = XP)
// ========================================
const LEVEL_THRESHOLDS = [
    { level: 1, min: 0, icon: '🌱', title: 'Beginner', color: '#22c55e' },
    { level: 2, min: 5, icon: '📖', title: 'Learner', color: '#3b82f6' },
    { level: 3, min: 10, icon: '✏️', title: 'Note Taker', color: '#06b6d4' },
    { level: 4, min: 20, icon: '🎓', title: 'Scholar', color: '#a78bfa' },
    { level: 5, min: 40, icon: '🧠', title: 'Expert', color: '#f97316' },
    { level: 6, min: 75, icon: '🏆', title: 'Master', color: '#ec4899' },
    { level: 7, min: 150, icon: '👑', title: 'Legend', color: '#facc15' },
];
const NOTES_COUNT_KEY = 'notewav_notes_processed_count';

function getNotesProcessedCount() {
    try {
        return parseInt(localStorage.getItem(NOTES_COUNT_KEY) || '0', 10) || 0;
    } catch (e) {
        return 0;
    }
}

function getLevelInfo(count) {
    let current = LEVEL_THRESHOLDS[0];
    for (const lvl of LEVEL_THRESHOLDS) {
        if (count >= lvl.min) current = lvl;
    }
    const currentIndex = LEVEL_THRESHOLDS.indexOf(current);
    const next = LEVEL_THRESHOLDS[currentIndex + 1] || null;
    return { ...current, next };
}

function renderLevelBadge() {
    const iconEl = document.getElementById('level-icon');
    const titleEl = document.getElementById('level-title');
    const badgeEl = document.getElementById('level-badge');
    const count = getNotesProcessedCount();
    const info = getLevelInfo(count);

    if (iconEl) iconEl.textContent = info.icon;
    if (titleEl) titleEl.textContent = info.title;
    if (badgeEl) {
        badgeEl.title = info.next
            ? `Level ${info.level}: ${info.title} — තව notes ${info.next.min - count}ක් process කළොත් "${info.next.title}" වෙනවා!`
            : `Level ${info.level}: ${info.title} — ඉහළම level එකට ළඟා වුනා! 🎉`;
    }

    // NEW: profile avatar ring color, matched to the current level.
    const avatarBtn = document.getElementById('profile-avatar-btn');
    if (avatarBtn) {
        avatarBtn.style.borderColor = info.color;
        avatarBtn.style.boxShadow = `0 0 0 2px ${info.color}22`;
    }

    // NEW: profile drawer — level label + progress bar toward next level.
    const profileLevelLabel = document.getElementById('profile-level-label');
    const profileProgressText = document.getElementById('profile-level-progress-text');
    const profileProgressFill = document.getElementById('profile-level-progress-fill');
    if (profileLevelLabel) profileLevelLabel.textContent = `${info.icon} ${info.title} (Level ${info.level})`;
    if (profileProgressFill) {
        let percent = 100;
        if (info.next) {
            const span = info.next.min - info.min;
            const progressed = count - info.min;
            percent = Math.min(100, Math.max(0, (progressed / span) * 100));
        }
        profileProgressFill.style.width = `${percent}%`;
        profileProgressFill.style.background = `linear-gradient(90deg, ${info.color}, ${info.color}cc)`;
    }
    if (profileProgressText) {
        profileProgressText.textContent = info.next
            ? `${count}/${info.next.min}`
            : 'MAX';
    }

    // NEW: header level badge popup content (current level + next level hint)
    const popupCurrentEl = document.getElementById('level-popup-current');
    const popupNextEl = document.getElementById('level-popup-next');
    if (popupCurrentEl) popupCurrentEl.textContent = `${info.icon} Level ${info.level}: ${info.title}`;
    if (popupNextEl) {
        popupNextEl.textContent = info.next
            ? `📈 තව notes ${info.next.min - count}ක් process කළොත් "${info.next.icon} ${info.next.title}" (Level ${info.next.level}) වෙනවා! (${count}/${info.next.min})`
            : `🎉 ඔබ ඉහළම level එකට ළඟා වුනා!`;
    }
}

document.addEventListener('DOMContentLoaded', function() {
    const levelBadgeBtn = document.getElementById('level-badge');
    const levelInfoPopup = document.getElementById('level-info-popup');
    if (levelBadgeBtn && levelInfoPopup) {
        levelBadgeBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            levelInfoPopup.classList.toggle('hidden');
        });
        document.addEventListener('click', function(e) {
            if (!levelInfoPopup.classList.contains('hidden') && !e.target.closest('#level-info-popup') && e.target !== levelBadgeBtn && !e.target.closest('#level-badge')) {
                levelInfoPopup.classList.add('hidden');
            }
        });
    }
});

function showLevelUpCelebration(newLevelInfo) {
    const overlay = document.getElementById('level-up-overlay');
    const iconEl = document.getElementById('level-up-icon');
    const headingEl = document.getElementById('level-up-heading');
    const subtextEl = document.getElementById('level-up-subtext');
    if (!overlay) return;

    // Reward: 5 free coins for reaching a new level.
    const LEVEL_UP_COINS_REWARD = 5;
    if (typeof addCoins === 'function') addCoins(LEVEL_UP_COINS_REWARD);
    if (typeof updateCoinsDisplay === 'function') updateCoinsDisplay();

    if (iconEl) iconEl.textContent = newLevelInfo.icon;
    if (headingEl) headingEl.textContent = `Level ${newLevelInfo.level}: ${newLevelInfo.title}!`;
    if (subtextEl) subtextEl.textContent = `🎉 Congrats! ඔබ notes process කරලා level up වුනා — 🪙 ${LEVEL_UP_COINS_REWARD} coins reward එකක්! ඉගෙනීම දිගටම කරගෙන යන්න!`;
    overlay.classList.add('show');
}

function incrementNotesProcessedCount() {
    const before = getNotesProcessedCount();
    const after = before + 1;
    try {
        localStorage.setItem(NOTES_COUNT_KEY, String(after));
    } catch (e) {
        console.warn('Could not save notes processed count:', e);
    }
    const beforeInfo = getLevelInfo(before);
    const afterInfo = getLevelInfo(after);
    renderLevelBadge();
    if (afterInfo.level > beforeInfo.level) {
        showLevelUpCelebration(afterInfo);
    }
}

document.addEventListener('DOMContentLoaded', function() {
    renderLevelBadge();
    const levelUpCloseBtn = document.getElementById('level-up-close-btn');
    const levelUpOverlay = document.getElementById('level-up-overlay');
    if (levelUpCloseBtn && levelUpOverlay) {
        levelUpCloseBtn.addEventListener('click', () => levelUpOverlay.classList.remove('show'));
    }
    if (levelUpOverlay) {
        levelUpOverlay.addEventListener('click', function(e) {
            if (e.target === levelUpOverlay) levelUpOverlay.classList.remove('show');
        });
    }
});

// ========================================
// THEME (Dark / Light toggle)
// ========================================
function applyTheme(theme) {
    document.body.classList.toggle('light-theme', theme === 'light');
    const darkBtn = document.getElementById('theme-dark-btn');
    const lightBtn = document.getElementById('theme-light-btn');
    if (darkBtn) darkBtn.classList.toggle('active', theme === 'dark');
    if (lightBtn) lightBtn.classList.toggle('active', theme === 'light');
    try {
        localStorage.setItem('notewav_theme', theme);
    } catch (e) {
        console.warn('Could not save theme preference:', e);
    }
}

document.addEventListener('DOMContentLoaded', function() {
    let savedTheme = 'dark';
    try {
        const stored = localStorage.getItem('notewav_theme');
        if (stored === 'light' || stored === 'dark') savedTheme = stored;
    } catch (e) {
        // ignore, default to dark
    }
    applyTheme(savedTheme);

    const themeDarkBtn = document.getElementById('theme-dark-btn');
    const themeLightBtn = document.getElementById('theme-light-btn');
    if (themeDarkBtn) themeDarkBtn.addEventListener('click', () => applyTheme('dark'));
    if (themeLightBtn) themeLightBtn.addEventListener('click', () => applyTheme('light'));
});

// ========================================
// OFFLINE AUDIO LIST (recently played, still playable offline)
// ========================================
// Relies on the service worker having already cached the audio file
// the first time it played (network-first strategy, falls back to
// cache when offline or after the server's hourly cleanup) — this just
// keeps a small, friendly list of what's available to replay.
const OFFLINE_AUDIO_KEY = 'notewav_offline_audio';
const OFFLINE_AUDIO_MAX = 10;

function getOfflineAudioList() {
    try {
        return JSON.parse(localStorage.getItem(OFFLINE_AUDIO_KEY) || '[]');
    } catch (e) {
        return [];
    }
}

function saveOfflineAudioEntry(sourceText, audioUrl) {
    if (!audioUrl) return;
    try {
        let list = getOfflineAudioList();
        list = list.filter(entry => entry.url !== audioUrl); // dedupe
        const title = (sourceText || 'Audio').trim().split(/\s+/).slice(0, 8).join(' ') || 'Audio';
        list.unshift({ title, url: audioUrl, date: new Date().toISOString() });
        list = list.slice(0, OFFLINE_AUDIO_MAX);
        localStorage.setItem(OFFLINE_AUDIO_KEY, JSON.stringify(list));
        renderOfflineAudioList();
    } catch (e) {
        console.warn('Could not save offline audio entry:', e);
    }
}

function removeOfflineAudioEntry(url) {
    try {
        let list = getOfflineAudioList().filter(entry => entry.url !== url);
        localStorage.setItem(OFFLINE_AUDIO_KEY, JSON.stringify(list));
        renderOfflineAudioList();
    } catch (e) {
        console.warn('Could not remove offline audio entry:', e);
    }
}

function offlineAudioEscapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

// Single shared <audio> reused across all offline-list items — only one
// track plays at a time, which keeps the play/pause + seek UI simple
// and matches how the main audio player already behaves.
const offlineAudioPlayer = new Audio();
let offlineAudioActiveUrl = null;

function offlineAudioFormatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '0:00';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
}

function offlineAudioSetPlayingUI(url, isPlaying) {
    document.querySelectorAll('.offline-audio-playpause-btn').forEach(btn => {
        const isThisOne = btn.dataset.url === url;
        btn.innerHTML = (isThisOne && isPlaying) ? '<i class="fas fa-pause"></i>' : '<i class="fas fa-play"></i>';
    });
}

function offlineAudioPlay(url) {
    if (offlineAudioActiveUrl !== url) {
        offlineAudioPlayer.src = url;
        offlineAudioActiveUrl = url;
    }
    offlineAudioPlayer.play().then(() => {
        offlineAudioSetPlayingUI(url, true);
    }).catch(err => {
        console.warn('Offline audio play failed:', err);
        alert('මේ audio එක තවම device එකේ save වෙලා නෑ (online තියෙන කොට එකපාරක් play කරන්න ඕන).\nThis audio hasn\'t been saved on this device yet — play it once while online first.');
    });
}

function offlineAudioPause() {
    offlineAudioPlayer.pause();
    offlineAudioSetPlayingUI(offlineAudioActiveUrl, false);
}

offlineAudioPlayer.addEventListener('timeupdate', function() {
    if (!offlineAudioActiveUrl) return;
    const seekInput = document.querySelector(`.offline-audio-seek[data-url="${offlineAudioActiveUrl}"]`);
    const currentLabel = document.querySelector(`.offline-audio-time-current[data-url="${offlineAudioActiveUrl}"]`);
    if (seekInput && this.duration) seekInput.value = (this.currentTime / this.duration) * 100;
    if (currentLabel) currentLabel.textContent = offlineAudioFormatTime(this.currentTime);
});
offlineAudioPlayer.addEventListener('loadedmetadata', function() {
    if (!offlineAudioActiveUrl) return;
    const totalLabel = document.querySelector(`.offline-audio-time-total[data-url="${offlineAudioActiveUrl}"]`);
    if (totalLabel) totalLabel.textContent = offlineAudioFormatTime(this.duration);
});
offlineAudioPlayer.addEventListener('ended', function() {
    offlineAudioSetPlayingUI(offlineAudioActiveUrl, false);
});

function renderOfflineAudioList() {
    const container = document.getElementById('offline-audio-list');
    if (!container) return;
    const list = getOfflineAudioList();
    if (!list.length) {
        container.innerHTML = '<p style="font-size: 0.78rem; color: var(--text-muted);">තවම audio එකක් play කරලා නෑ.</p>';
        return;
    }
    container.innerHTML = list.map(entry => {
        const dateStr = new Date(entry.date).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
        return `
            <div class="offline-audio-item" style="padding: 10px 0; border-bottom: 1px solid var(--border-color);">
                <div style="display: flex; align-items: center; gap: 10px;">
                    <button class="offline-audio-playpause-btn" data-url="${entry.url}" style="background: none; border: none; color: #a78bfa; cursor: pointer; font-size: 1.1rem; padding: 4px; flex-shrink: 0;"><i class="fas fa-play"></i></button>
                    <div style="flex: 1; min-width: 0;">
                        <div style="font-size: 0.82rem; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${offlineAudioEscapeHtml(entry.title)}</div>
                        <div style="font-size: 0.68rem; color: var(--text-muted);">${dateStr}</div>
                    </div>
                    <button class="offline-audio-remove-btn" data-url="${entry.url}" style="background: none; border: none; color: #f87171; cursor: pointer; padding: 4px; flex-shrink: 0;"><i class="fas fa-times"></i></button>
                </div>
                <div style="display: flex; align-items: center; gap: 8px; margin-top: 6px;">
                    <span class="offline-audio-time-current" data-url="${entry.url}" style="font-size: 0.65rem; color: var(--text-muted); min-width: 30px;">0:00</span>
                    <input type="range" class="offline-audio-seek" data-url="${entry.url}" min="0" max="100" value="0" step="0.1" style="flex: 1; accent-color: #6b30ff; height: 4px; cursor: pointer;">
                    <span class="offline-audio-time-total" data-url="${entry.url}" style="font-size: 0.65rem; color: var(--text-muted); min-width: 30px;">0:00</span>
                </div>
            </div>`;
    }).join('');

    container.querySelectorAll('.offline-audio-playpause-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const url = this.dataset.url;
            if (offlineAudioActiveUrl === url && !offlineAudioPlayer.paused) {
                offlineAudioPause();
            } else {
                offlineAudioPlay(url);
            }
        });
    });
    container.querySelectorAll('.offline-audio-seek').forEach(input => {
        input.addEventListener('input', function() {
            const url = this.dataset.url;
            if (offlineAudioActiveUrl !== url) {
                offlineAudioPlayer.src = url;
                offlineAudioActiveUrl = url;
            }
            if (offlineAudioPlayer.duration) {
                offlineAudioPlayer.currentTime = (this.value / 100) * offlineAudioPlayer.duration;
            }
        });
    });
    container.querySelectorAll('.offline-audio-remove-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const url = this.dataset.url;
            if (offlineAudioActiveUrl === url) {
                offlineAudioPause();
                offlineAudioActiveUrl = null;
            }
            removeOfflineAudioEntry(url);
        });
    });
}

document.addEventListener('DOMContentLoaded', renderOfflineAudioList);

document.addEventListener('DOMContentLoaded', function() {
    const offlineAudioToggleBtn = document.getElementById('offline-audio-toggle-btn');
    const offlineAudioWrap = document.getElementById('offline-audio-wrap');
    if (offlineAudioToggleBtn && offlineAudioWrap) {
        offlineAudioToggleBtn.addEventListener('click', function() {
            const isHidden = offlineAudioWrap.classList.toggle('hidden');
            this.classList.toggle('active-toggle', !isHidden);
        });
    }
});

function applyAppLanguage(lang) {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        const entry = NOTEWAV_TRANSLATIONS[key];
        if (entry && entry[lang]) el.textContent = entry[lang];
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        const key = el.getAttribute('data-i18n-placeholder');
        const entry = NOTEWAV_TRANSLATIONS[key];
        if (entry && entry[lang]) el.setAttribute('placeholder', entry[lang]);
    });
    document.querySelectorAll('#app-lang-si-btn, #app-lang-en-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.applang === lang);
    });
    try {
        localStorage.setItem('notewav_app_language', lang);
    } catch (e) {
        console.warn('Could not save app language preference:', e);
    }
    if (typeof updateGreeting === 'function') updateGreeting();
}

document.addEventListener('DOMContentLoaded', function() {
    let savedLang = 'si';
    try {
        const stored = localStorage.getItem('notewav_app_language');
        if (stored === 'si' || stored === 'en') savedLang = stored;
    } catch (e) {
        // ignore, use default
    }
    applyAppLanguage(savedLang);

    const appLangSiBtn = document.getElementById('app-lang-si-btn');
    const appLangEnBtn = document.getElementById('app-lang-en-btn');
    if (appLangSiBtn) appLangSiBtn.addEventListener('click', () => applyAppLanguage('si'));
    if (appLangEnBtn) appLangEnBtn.addEventListener('click', () => applyAppLanguage('en'));
});

// ========================================
// LOCAL PROFILE (name + avatar photo)
// ========================================
document.addEventListener('DOMContentLoaded', function() {
    const PROFILE_KEY = 'notewav_profile';
    const avatarBtn = document.getElementById('profile-avatar-btn');
    const avatarImg = document.getElementById('profile-avatar-img');
    const avatarPlaceholder = document.getElementById('profile-avatar-placeholder');
    const avatarInput = document.getElementById('profile-avatar-input');
    const nameInput = document.getElementById('profile-name-input');
    const nameDisplay = document.getElementById('profile-name-display');
    const nameEditBtn = document.getElementById('profile-name-edit-btn');
    const nameEditRow = document.getElementById('profile-name-edit-row');
    const nameSaveBtn = document.getElementById('profile-name-save-btn');
    if (!avatarBtn || !avatarImg || !avatarInput || !nameInput) return;

    function refreshNameDisplay() {
        try {
            const data = JSON.parse(localStorage.getItem(PROFILE_KEY) || '{}');
            if (nameDisplay) nameDisplay.textContent = data.name || 'ඔබේ නම (Your name)';
        } catch (e) {
            if (nameDisplay) nameDisplay.textContent = 'ඔබේ නම (Your name)';
        }
    }

    function loadProfile() {
        try {
            const data = JSON.parse(localStorage.getItem(PROFILE_KEY) || '{}');
            if (data.name) nameInput.value = data.name;
            if (data.avatarDataUrl) {
                avatarImg.src = data.avatarDataUrl;
                avatarImg.classList.remove('hidden');
                if (avatarPlaceholder) avatarPlaceholder.classList.add('hidden');
            }
        } catch (e) {
            console.warn('Could not load saved profile:', e);
        }
        refreshNameDisplay();
    }

    function saveProfile(partial) {
        try {
            const current = JSON.parse(localStorage.getItem(PROFILE_KEY) || '{}');
            const updated = Object.assign({}, current, partial);
            localStorage.setItem(PROFILE_KEY, JSON.stringify(updated));
        } catch (e) {
            console.warn('Could not save profile:', e);
        }
        refreshNameDisplay();
    }

    function resizeImageToDataUrl(file, maxDim) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = function(e) {
                const img = new Image();
                img.onload = function() {
                    const canvas = document.createElement('canvas');
                    const scale = Math.min(1, maxDim / Math.max(img.width, img.height));
                    canvas.width = Math.round(img.width * scale);
                    canvas.height = Math.round(img.height * scale);
                    const ctx = canvas.getContext('2d');
                    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                    resolve(canvas.toDataURL('image/jpeg', 0.85));
                };
                img.onerror = reject;
                img.src = e.target.result;
            };
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    }

    avatarBtn.addEventListener('click', function() {
        avatarInput.click();
    });

    avatarInput.addEventListener('change', async function() {
        const file = this.files && this.files[0];
        this.value = '';
        if (!file || !file.type.startsWith('image/')) return;
        try {
            const dataUrl = await resizeImageToDataUrl(file, 200);
            avatarImg.src = dataUrl;
            avatarImg.classList.remove('hidden');
            if (avatarPlaceholder) avatarPlaceholder.classList.add('hidden');
            saveProfile({ avatarDataUrl: dataUrl });
        } catch (e) {
            console.warn('Profile photo processing failed:', e);
        }
    });

    // NEW: name is now display-only by default, with a pencil icon to
    // enter edit mode and an explicit Save (✓) button to commit — a
    // clearer, more deliberate flow than "every keystroke auto-saves
    // silently." Saving here ALSO marks the name as manually chosen
    // (nameManuallySet: true), so a later Google Sign-In never
    // silently overwrites a name the person specifically picked.
    if (nameEditBtn && nameEditRow) {
        nameEditBtn.addEventListener('click', function() {
            nameEditRow.classList.remove('hidden');
            nameInput.focus();
        });
    }

    function commitNameEdit() {
        const value = nameInput.value.trim();
        saveProfile({ name: value, nameManuallySet: true });
        if (nameEditRow) nameEditRow.classList.add('hidden');
        if (typeof updateGreeting === 'function') updateGreeting();
        // If signed in, persist this to the account too — so it
        // follows the person to any other device they log into,
        // instead of only ever living in this browser's localStorage.
        if (typeof notewavIsLoggedIn !== 'undefined' && notewavIsLoggedIn) {
            fetch('/user/update-profile', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: value }),
            }).catch(() => { /* best-effort only */ });
        }
    }

    if (nameSaveBtn) nameSaveBtn.addEventListener('click', commitNameEdit);
    nameInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') commitNameEdit();
    });

    loadProfile();
});

// ========================================
// TIME-BASED GREETING (uses saved profile name)
// ========================================
const NOTEWAV_GREETINGS = {
    morning: { si: 'සුබ උදෑසනක්', en: 'Good Morning' },
    afternoon: { si: 'සුබ දහවලක්', en: 'Good Afternoon' },
    evening: { si: 'සුබ සන්ධ්‍යාවක්', en: 'Good Evening' },
    night: { si: 'සුබ රාත්‍රියක්', en: 'Good Night' },
};

function updateGreeting() {
    const greetingEl = document.getElementById('greeting-text');
    if (!greetingEl) return;

    function getGreetingPeriod() {
        const hour = new Date().getHours();
        if (hour >= 5 && hour < 12) return 'morning';
        if (hour >= 12 && hour < 17) return 'afternoon';
        if (hour >= 17 && hour < 21) return 'evening';
        return 'night';
    }

    let lang = 'si';
    try {
        const storedLang = localStorage.getItem('notewav_app_language');
        if (storedLang === 'si' || storedLang === 'en') lang = storedLang;
    } catch (e) {
        // ignore, default to 'si'
    }

    let name = '';
    try {
        const profile = JSON.parse(localStorage.getItem('notewav_profile') || '{}');
        if (profile.name) name = profile.name.trim();
    } catch (e) {
        // ignore, no name
    }

    const period = getGreetingPeriod();
    const greetingWord = NOTEWAV_GREETINGS[period][lang];
    greetingEl.textContent = name ? `${greetingWord}, ${name}! 👋` : `${greetingWord}! 👋`;
    greetingEl.classList.remove('hidden');
}

document.addEventListener('DOMContentLoaded', updateGreeting);

// ========================================
// FIRST-VISIT WELCOME CARD (asks for the person's name once)
// ========================================
document.addEventListener('DOMContentLoaded', function() {
    const PROFILE_KEY = 'notewav_profile';
    const ONBOARDING_DONE_KEY = 'notewav_onboarding_done';

    const welcomeBackdrop = document.getElementById('welcome-modal-backdrop');
    const welcomeNameInput = document.getElementById('welcome-name-input');
    const welcomeSaveBtn = document.getElementById('welcome-save-btn');
    const welcomeSkipBtn = document.getElementById('welcome-skip-btn');
    if (!welcomeBackdrop || !welcomeNameInput || !welcomeSaveBtn || !welcomeSkipBtn) return;

    let existingName = '';
    let onboardingDone = false;
    try {
        const profile = JSON.parse(localStorage.getItem(PROFILE_KEY) || '{}');
        existingName = (profile.name || '').trim();
        onboardingDone = localStorage.getItem(ONBOARDING_DONE_KEY) === 'true';
    } catch (e) {
        onboardingDone = true;
    }

    if (existingName || onboardingDone) {
        return;
    }

    setTimeout(() => {
        welcomeBackdrop.classList.remove('hidden');
        welcomeNameInput.focus();
    }, 2200);

    function markOnboardingDone() {
        try {
            localStorage.setItem(ONBOARDING_DONE_KEY, 'true');
        } catch (e) {
            console.warn('Could not save onboarding state:', e);
        }
    }

    function closeWelcomeModal() {
        welcomeBackdrop.classList.add('hidden');
    }

    function saveNameAndClose() {
        const name = welcomeNameInput.value.trim();
        if (name) {
            try {
                const current = JSON.parse(localStorage.getItem(PROFILE_KEY) || '{}');
                current.name = name;
                current.nameManuallySet = true;
                localStorage.setItem(PROFILE_KEY, JSON.stringify(current));
            } catch (e) {
                console.warn('Could not save name:', e);
            }
            const drawerNameInput = document.getElementById('profile-name-input');
            if (drawerNameInput) drawerNameInput.value = name;
            const drawerNameDisplay = document.getElementById('profile-name-display');
            if (drawerNameDisplay) drawerNameDisplay.textContent = name;
            updateGreeting();
        }
        markOnboardingDone();
        closeWelcomeModal();
    }

    welcomeSaveBtn.addEventListener('click', saveNameAndClose);
    welcomeNameInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') saveNameAndClose();
    });
    welcomeSkipBtn.addEventListener('click', function() {
        markOnboardingDone();
        closeWelcomeModal();
    });
});

// ========================================
// STUDY TIME TRACKER (minutes listened today)
// ========================================
let lastTrackedAudioTime = null;

function todayLocalDateString() {
    const now = new Date();
    const y = now.getFullYear();
    const m = String(now.getMonth() + 1).padStart(2, '0');
    const d = String(now.getDate()).padStart(2, '0');
    return `${y}-${m}-${d}`;
}

function trackStudyTime(currentAudioTime) {
    if (lastTrackedAudioTime !== null) {
        const delta = currentAudioTime - lastTrackedAudioTime;
        if (delta > 0 && delta < 2) {
            addStudyMinutes(delta / 60);
        }
    }
    lastTrackedAudioTime = currentAudioTime;
}

function addStudyMinutes(minutesToAdd) {
    try {
        const STUDY_TIME_KEY = 'notewav_study_time';
        const today = todayLocalDateString();
        let data = JSON.parse(localStorage.getItem(STUDY_TIME_KEY) || '{}');
        if (data.date !== today) {
            data = { date: today, minutes: 0 };
        }
        data.minutes = (data.minutes || 0) + minutesToAdd;
        localStorage.setItem(STUDY_TIME_KEY, JSON.stringify(data));
        updateStudyTimeDisplay(data.minutes);
    } catch (e) {
        console.warn('Could not track study time:', e);
    }
}

function updateStudyTimeDisplay(minutes) {
    const el = document.getElementById('study-time-value');
    if (el) el.textContent = Math.round(minutes) + ' min';
}

document.addEventListener('DOMContentLoaded', function() {
    try {
        const STUDY_TIME_KEY = 'notewav_study_time';
        const today = todayLocalDateString();
        const data = JSON.parse(localStorage.getItem(STUDY_TIME_KEY) || '{}');
        updateStudyTimeDisplay(data.date === today ? (data.minutes || 0) : 0);
    } catch (e) {
        updateStudyTimeDisplay(0);
    }
});

// ========================================
// VOICE INPUT (speak instead of type)
// ========================================
document.addEventListener('DOMContentLoaded', function() {
    const voiceBtn = document.getElementById('note-input-voice-btn');
    const targetNoteInput = document.getElementById('note-input');
    if (!voiceBtn || !targetNoteInput) return;

    const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognitionCtor) {
        voiceBtn.style.display = 'none';
        return;
    }

    let recognition = null;
    let isListening = false;

    function startListening() {
        recognition = new SpeechRecognitionCtor();
        recognition.lang = 'si-LK';
        recognition.continuous = true;
        recognition.interimResults = false;

        recognition.onstart = function() {
            isListening = true;
            voiceBtn.classList.add('copied');
            trackUsageEvent('voice_input_used');
        };
        recognition.onresult = function(event) {
            let transcript = '';
            for (let i = event.resultIndex; i < event.results.length; i++) {
                transcript += event.results[i][0].transcript;
            }
            if (transcript.trim()) {
                const existing = targetNoteInput.value;
                const needsSpace = existing && !existing.endsWith(' ') && !existing.endsWith('\n');
                targetNoteInput.value = existing + (needsSpace ? ' ' : '') + transcript.trim() + ' ';
                targetNoteInput.dispatchEvent(new Event('input'));
            }
        };
        recognition.onerror = function(event) {
            console.warn('Speech recognition error:', event.error);
            stopListening();
            if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
                showErrorBanner('Microphone access ලබා දෙන්න ඕන Voice Input use කරන්න.');
            }
        };
        recognition.onend = function() {
            stopListening();
        };
        recognition.start();
    }

    function stopListening() {
        isListening = false;
        voiceBtn.classList.remove('copied');
        if (recognition) {
            try { recognition.stop(); } catch (e) { /* already stopped */ }
        }
    }

    voiceBtn.addEventListener('click', function() {
        if (isListening) {
            stopListening();
        } else {
            startListening();
        }
    });
});

// ========================================
// LIBRARY BACKUP (Export / Import as JSON)
// ========================================
document.addEventListener('DOMContentLoaded', function() {
    const exportBtn = document.getElementById('library-export-btn');
    const importBtn = document.getElementById('library-import-btn');
    const importInput = document.getElementById('library-import-input');
    if (!exportBtn || !importBtn || !importInput) return;

    exportBtn.addEventListener('click', async function() {
        trackUsageEvent('library_exported');
        try {
            const res = await fetch('/library/export');
            const data = await res.json();
            if (data.status !== 'success') {
                showErrorBanner('Export කිරීම අසාර්ථක විය.');
                return;
            }
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            const dateStr = todayLocalDateString();
            link.download = `notewav_library_backup_${dateStr}.json`;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);
        } catch (e) {
            console.error('Library export failed:', e);
            showErrorBanner('Export කිරීම අසාර්ථක විය: ' + e.message);
        }
    });

    importBtn.addEventListener('click', function() {
        importInput.click();
    });

    importInput.addEventListener('change', async function() {
        const file = this.files && this.files[0];
        this.value = '';
        if (!file) return;

        try {
            const text = await file.text();
            const parsed = JSON.parse(text);
            const notes = parsed.notes;
            if (!Array.isArray(notes) || notes.length === 0) {
                showErrorBanner('Import file එකේ notes හමු නොවීය.');
                return;
            }

            let successCount = 0;
            let profileNameForImport = '';
            try {
                const profile = JSON.parse(localStorage.getItem('notewav_profile') || '{}');
                if (profile.name) profileNameForImport = profile.name.trim();
            } catch (e) { /* ignore */ }

            for (const note of notes) {
                try {
                    const res = await fetch('/library/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            subject: note.subject || 'General',
                            note_text: note.note_text || '',
                            processed_text: note.processed_text || '',
                            mermaid_code_si: note.mermaid_code_si || '',
                            mermaid_code_en: note.mermaid_code_en || '',
                            mode: note.mode || 'full',
                            anon_id: (typeof getOrCreateAnonId === 'function') ? getOrCreateAnonId() : '',
                            user_name: profileNameForImport,
                        }),
                    });
                    const result = await res.json();
                    if (result.status === 'success') successCount++;
                } catch (innerErr) {
                    console.warn('One note failed to import:', innerErr);
                }
            }

            showErrorBanner(`${successCount} / ${notes.length} notes import කරගන්නා ලදී. Page එක refresh වෙනවා...`);
            setTimeout(() => window.location.reload(), 1800);
        } catch (e) {
            console.error('Library import failed:', e);
            showErrorBanner('Import කිරීම අසාර්ථක විය: ' + e.message);
        }
    });
});

// ========================================
// COINS SYSTEM (placeholder — for future paid features)
// ========================================
const COINS_KEY = 'notewav_coins';
const STARTING_FREE_COINS = 100;

function getCoinsBalance() {
    try {
        const stored = localStorage.getItem(COINS_KEY);
        if (stored === null) {
            localStorage.setItem(COINS_KEY, String(STARTING_FREE_COINS));
            return STARTING_FREE_COINS;
        }
        return parseInt(stored, 10) || 0;
    } catch (e) {
        return STARTING_FREE_COINS;
    }
}

function updateCoinsDisplay() {
    const el = document.getElementById('coins-count');
    // NEW: show a plain "Free" label instead of the actual number —
    // the underlying coin balance (getCoinsBalance()) still tracks
    // normally in localStorage for future paid-feature gating, this
    // only changes what's DISPLAYED on the badge.
    if (el) el.textContent = 'Free';
}

function spendCoins(amount) {
    const current = getCoinsBalance();
    if (current < amount) return false;
    try {
        localStorage.setItem(COINS_KEY, String(current - amount));
    } catch (e) {
        console.warn('Could not update coins balance:', e);
    }
    updateCoinsDisplay();
    syncAccountToServer({ coins: current - amount });
    return true;
}

function addCoins(amount) {
    const current = getCoinsBalance();
    try {
        localStorage.setItem(COINS_KEY, String(current + amount));
    } catch (e) {
        console.warn('Could not update coins balance:', e);
    }
    updateCoinsDisplay();
    syncAccountToServer({ coins: current + amount });
}

document.addEventListener('DOMContentLoaded', updateCoinsDisplay);

// ========================================
// FOOTER INFO MODALS (About / Support / Privacy)
// ========================================
document.addEventListener('DOMContentLoaded', function() {
    const infoModalBackdrop = document.getElementById('info-modal-backdrop');
    const infoModalClose = document.getElementById('info-modal-close');
    const infoModalTitle = document.getElementById('info-modal-title');
    const infoModalBody = document.getElementById('info-modal-body');
    if (!infoModalBackdrop || !infoModalTitle || !infoModalBody) return;

    const INFO_CONTENT = {
        about: {
            si: {
                title: 'ℹ️ NoteWav AI ගැන — App එක Use කරන විදිහ',
                html: `
                    <h4>1️⃣ පාඩම් සටහන ඇතුළත් කරන්න</h4>
                    <p>ඔබේ පාඩම් සටහන type කරන්න, හෝ පාඩමේ රූපයක් (photo) upload කරන්න — app එක automatic ලෙස රූපයෙන් text එක උපුටාගනියි (OCR).</p>
                    <h4>2️⃣ Study Mode එකක් තෝරන්න</h4>
                    <p><b>Smart Study:</b> AI මගින් podcast script එකක් සහ mind map එකක් සකසනවා.<br><b>Full Text Mode:</b> ඔබේ text එකම audio + mind map බවට හැරවෙනවා.</p>
                    <h4>3️⃣ "Script එක සකසන්න" click කරන්න</h4>
                    <p>AI එක ඔබේ සටහන process කරයි. ඕන නම් script එක Edit කරන්න (Safety Check step එකේදී).</p>
                    <h4>4️⃣ Audio Generate කරන්න</h4>
                    <p>"තරංග උත්පාදනය කරන්න" click කරන්න — podcast-style audio එකක් සකසේවි, play/pause/speed/skip controls සමඟ.</p>
                    <h4>5️⃣ Mind Map එක බලන්න</h4>
                    <p>Visual mind map එකක් auto-generate වේ — click කර විශාල කර, PNG/PDF විදිහට download කරගන්න පුළුවන්.</p>
                    <h4>6️⃣ Library එකට Save කරන්න</h4>
                    <p>ඔබේ notes පසුව use කරන්න Library එකට save කරගන්න, subject එකකින් organize කරගන්න.</p>
                `,
            },
            en: {
                title: 'ℹ️ About NoteWav AI — How to Use the App',
                html: `
                    <h4>1️⃣ Enter Your Note</h4>
                    <p>Type your study note, or upload a photo of your lesson — the app automatically extracts the text from the image (OCR).</p>
                    <h4>2️⃣ Choose a Study Mode</h4>
                    <p><b>Smart Study:</b> AI prepares a podcast script and a mind map for you.<br><b>Full Text Mode:</b> Your own text becomes the audio + mind map directly.</p>
                    <h4>3️⃣ Click "Prepare Script"</h4>
                    <p>The AI processes your note. You can edit the script if needed (during the Safety Check step).</p>
                    <h4>4️⃣ Generate Audio</h4>
                    <p>Click "Generate Audio" — a podcast-style narration is created, with play/pause/speed/skip controls.</p>
                    <h4>5️⃣ View the Mind Map</h4>
                    <p>A visual mind map is auto-generated — click to enlarge it, and download it as a PNG or PDF.</p>
                    <h4>6️⃣ Save to Library</h4>
                    <p>Save your notes to the Library to use later, organized by subject.</p>
                `,
            },
        },
        support: {
            si: {
                title: '📩 Support — උදව්වක් ඕනද?',
                html: `
                    <p>App එකේ ගැටලුවක් තිබේ නම්, feature request එකක් තිබේ නම්, හෝ වෙන කිසිම දෙයක් ගැන කතා කරන්න ඕන නම්, කරුණාකර පහත number එකට SMS/text message එකක් යවන්න:</p>
                    <div class="info-contact-box">
                        <i class="fas fa-mobile-screen"></i>
                        <div>
                            <div>Sandun (SCD)</div>
                            <div class="phone-number">+94 77 634 0009</div>
                        </div>
                    </div>
                    <a href="sms:+94776340009" class="info-sms-btn"><i class="fas fa-comment-sms"></i> Text Message එකක් යවන්න</a>
                `,
            },
            en: {
                title: '📩 Support — Need Help?',
                html: `
                    <p>If you run into an issue with the app, have a feature request, or want to talk about anything else, please send an SMS/text message to the number below:</p>
                    <div class="info-contact-box">
                        <i class="fas fa-mobile-screen"></i>
                        <div>
                            <div>Sandun (SCD)</div>
                            <div class="phone-number">+94 77 634 0009</div>
                        </div>
                    </div>
                    <a href="sms:+94776340009" class="info-sms-btn"><i class="fas fa-comment-sms"></i> Send a Text Message</a>
                `,
            },
        },
        privacy: {
            si: {
                title: '🔒 Privacy — ඔබේ දත්ත ගැන',
                html: `
                    <p>NoteWav AI විසින් login/account එකක් අවශ්‍ය නොකරයි. පහත දේවල් දැනගන්න:</p>
                    <p><b>ඔබේ device එකේම (localStorage) save වන දේවල්:</b> Profile name/photo, study streak, font size preference, language preference — මේවා අපගේ server එකට යවන්නේ නෑ.</p>
                    <p><b>Server එකට යවන දේවල්:</b> Note text (AI processing සඳහා), Library එකට save කරන notes, generate කරන audio/mind map. Library data එක Render server එකේ temporary storage එකක save වේ.</p>
                    <p><b>Usage tracking:</b> App එක use කරන බව (anonymous device ID එකක් + ඔබ ලබාදෙන නම, තිබේ නම්) admin ට usage patterns බලාගන්න track කෙරේ — password, email වැනි කිසිදු sensitive දත්තයක් රැස් නොකෙරේ.</p>
                `,
            },
            en: {
                title: '🔒 Privacy — About Your Data',
                html: `
                    <p>NoteWav AI does not require a login/account. Here's what you should know:</p>
                    <p><b>Saved only on your own device (localStorage):</b> Profile name/photo, study streak, font size preference, language preference — these are never sent to our server.</p>
                    <p><b>Sent to the server:</b> Note text (for AI processing), notes you save to the Library, generated audio/mind maps. Library data is stored in temporary storage on the Render server.</p>
                    <p><b>Usage tracking:</b> That you use the app (an anonymous device ID + the name you provide, if any) is tracked so the admin can see usage patterns — no sensitive data like passwords or emails is collected.</p>
                `,
            },
        },
    };

    function getCurrentInfoLang() {
        try {
            const stored = localStorage.getItem('notewav_app_language');
            return (stored === 'en') ? 'en' : 'si';
        } catch (e) {
            return 'si';
        }
    }

    function openInfoModal(key) {
        const content = INFO_CONTENT[key];
        if (!content) return;
        const lang = getCurrentInfoLang();
        const localized = content[lang] || content.si;
        infoModalTitle.textContent = localized.title;
        infoModalBody.innerHTML = localized.html;
        infoModalBackdrop.classList.remove('hidden');
    }

    const aboutLink = document.getElementById('about-link');
    const supportLink = document.getElementById('support-link');
    if (aboutLink) aboutLink.addEventListener('click', e => { e.preventDefault(); openInfoModal('about'); });
    if (supportLink) supportLink.addEventListener('click', e => { e.preventDefault(); openInfoModal('support'); });
    // NOTE: 'privacy-link' no longer opens a modal — it's now a real
    // link to the /privacy page (required for AdSense/AdMob approval,
    // which needs a genuinely accessible URL, not an in-app modal).

    if (infoModalClose) infoModalClose.addEventListener('click', () => infoModalBackdrop.classList.add('hidden'));
    infoModalBackdrop.addEventListener('click', function(e) {
        if (e.target === infoModalBackdrop) infoModalBackdrop.classList.add('hidden');
    });
});

// ========================================
// NOTIFICATION BELL (admin → users broadcast)
// ========================================
document.addEventListener('DOMContentLoaded', function() {
    const SEEN_KEY = 'notewav_last_seen_announcement_id';
    const bellBtn = document.getElementById('notification-bell-btn');
    const badge = document.getElementById('notification-badge');
    const popup = document.getElementById('notification-popup');
    const popupMessage = document.getElementById('notification-popup-message');
    const popupTimeText = document.getElementById('notification-popup-time-text');
    const popupClose = document.getElementById('notification-popup-close');
    if (!bellBtn || !badge || !popup) return;

    let latestAnnouncement = null;

    function getSeenId() {
        try {
            return parseInt(localStorage.getItem(SEEN_KEY) || '0', 10);
        } catch (e) {
            return 0;
        }
    }

    function markSeen(id) {
        try {
            localStorage.setItem(SEEN_KEY, String(id));
        } catch (e) { /* ignore */ }
        badge.classList.add('hidden');
    }

    async function checkForAnnouncement() {
        try {
            const anonId = (typeof getOrCreateAnonId === 'function') ? getOrCreateAnonId() : '';
            const res = await fetch('/announcements/latest?anon_id=' + encodeURIComponent(anonId));
            const data = await res.json();
            // TEMP DEBUG — remove once the notification issue is
            // confirmed fixed. Open DevTools (F12) → Console on the
            // STUDENT device to see exactly what the server returns
            // for this device's own anon_id.
            console.log('[NoteWav debug] my anon_id:', anonId, '| server response:', data, '| my seen_id:', getSeenId());
            if (data.status !== 'success' || !data.announcement) return;
            const isNew = data.announcement.id > getSeenId();
            latestAnnouncement = data.announcement;
            if (isNew) {
                badge.classList.remove('hidden');
                // Auto-popup removed per request — badge dot only now.
                // Person clicks the bell to actually read the message.
            }
        } catch (e) {
            // silent — notifications are non-critical
        }
    }

    bellBtn.addEventListener('click', function() {
        if (!latestAnnouncement) {
            popup.classList.toggle('hidden');
            return;
        }
        popupMessage.textContent = latestAnnouncement.message;
        try {
            const dt = new Date(latestAnnouncement.created_at);
            popupTimeText.textContent = dt.toLocaleString();
        } catch (e) {
            popupTimeText.textContent = '';
        }
        popup.classList.toggle('hidden');
        markSeen(latestAnnouncement.id);
    });

    if (popupClose) {
        popupClose.addEventListener('click', function(e) {
            e.stopPropagation();
            popup.classList.add('hidden');
        });
    }

    document.addEventListener('click', function(e) {
        if (!popup.contains(e.target) && !bellBtn.contains(e.target)) {
            popup.classList.add('hidden');
        }
    });

    checkForAnnouncement();
    // FIX (cost/scale concern): this was temporarily set very
    // aggressive (3s) while debugging a notification-delay bug. That
    // bug's ROOT CAUSE (a broken Turso connection) is now fixed, so
    // this no longer needs to poll so frequently — the visibilitychange
    // + window focus listeners below already catch the moment someone
    // actually looks at the app, which was the real goal. Polling
    // every 3s per device, multiplied across many simultaneous
    // students, adds up fast against Turso's free-tier monthly
    // rows-read limit. 20s is still responsive enough for a
    // notification, without the multiplied cost.
    setInterval(checkForAnnouncement, 20000); // check every 20 seconds
    document.addEventListener('visibilitychange', function() {
        if (document.visibilityState === 'visible') {
            checkForAnnouncement();
        }
    });
    window.addEventListener('focus', checkForAnnouncement);
});