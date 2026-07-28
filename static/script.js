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
// "anon_id" is a random ID generated once and stored in this
// browser's localStorage — NOT a real account/login, just a way to
// tell "this same device visited again" from "a different device".
// Paired with whatever display name the person entered (if any) so
// an admin can see, e.g., "Kasun's device processed 5 notes" without
// requiring students to actually sign in anywhere.
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

        fetch('/track', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                anon_id: getOrCreateAnonId(),
                user_name: userName,
                action: action,
            }),
        }).catch(() => { /* tracking is best-effort only, never disrupt the actual feature */ });
    } catch (e) {
        // ignore — tracking must never break the app
    }
}

document.addEventListener('DOMContentLoaded', function() {
    trackUsageEvent('app_opened');
});

document.addEventListener('DOMContentLoaded', function() {
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

    // NEW: remember the student's preferred speed across sessions —
    // previously it always reset to the default (1.25x) every time,
    // even if they'd changed it last time.
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
    let isPlaying = false;
    let isOCRRunning = false;
    let highlightUnits = [];
    let playbackSpeed = 1;
    let playbackVolume = 1;

    // gTTS's base voice reads a bit slower than feels natural for a
    // study podcast, and gTTS itself has no "faster" generation option
    // — so instead, permanently boost the ACTUAL applied playback rate
    // a bit above whatever speed button is selected. The button labels
    // (0.75x, 1x, 1.25x, 1.5x) stay the same for the student to
    // understand, but "1x" now genuinely sounds like a comfortable
    // normal pace instead of sluggish.
    const SPEED_BOOST_MULTIPLIER = 1.15;
    function getEffectivePlaybackRate() {
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

    async function loadLibraryList() {
        libraryModalBody.innerHTML = '<p class="mindmap-empty"><span class="mini-wave"><span></span><span></span><span></span><span></span></span> Loading...</p>';
        if (librarySearchInput) librarySearchInput.value = '';
        try {
            const response = await fetch('/library/notes');
            const data = await response.json();
            if (data.status !== 'success') {
                libraryModalBody.innerHTML = '<p class="mindmap-empty">Library එක load කරගැනීම අසාර්ථක විය.</p>';
                return;
            }
            allLibraryNotes = data.notes || [];
            renderLibraryList(allLibraryNotes);
        } catch (err) {
            console.error('Library load error:', err);
            libraryModalBody.innerHTML = '<p class="mindmap-empty">Library එක load කරගැනීම අසාර්ථක විය.</p>';
        }
    }

    function filterLibraryNotes(searchTerm) {
        const term = searchTerm.trim().toLowerCase();
        if (!term) {
            renderLibraryList(allLibraryNotes);
            return;
        }
        const filtered = allLibraryNotes.filter(note =>
            (note.title || '').toLowerCase().includes(term) ||
            (note.subject || '').toLowerCase().includes(term)
        );
        renderLibraryList(filtered, term);
    }

    if (librarySearchInput) {
        librarySearchInput.addEventListener('input', function() {
            filterLibraryNotes(this.value);
        });
    }

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
                html += `
                    <div class="library-note-item">
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
            const subject = (librarySubjectInput.value || 'General').trim() || 'General';
            const mode = document.querySelector('input[name="study_mode"]:checked').value;

            saveToLibraryBtn.disabled = true;
            saveToLibraryBtn.innerHTML = '<span class="mini-wave"><span></span><span></span><span></span><span></span></span> Saving...';

            try {
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

    // NEW: include a link back to the app itself in every share —
    // when a student shares a note with a friend, the friend also
    // gets a direct link to try NoteWav themselves (word-of-mouth
    // growth), not just the note content.
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

    // NEW: "Audio එකත් සමඟ Share කරන්න" — the wa.me/t.me links above can
    // only carry TEXT, not an actual audio file. To share the real
    // audio, this uses the Web Share API (navigator.share) with the
    // audio file attached, which opens the device's native share sheet
    // — from there the student can pick WhatsApp, Telegram, or
    // anything else, and the actual .mp3 goes along with the text.
    // Not supported on most desktop browsers, so it's offered as an
    // extra option alongside the text-only buttons, not a replacement.
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
    // FIX (highlight timing was noticeably off): the old version
    // assumed every sentence/line takes an EQUAL share of the total
    // audio duration — very wrong for a mix of short and long
    // sentences. Now uses the REAL per-sentence duration measured
    // during TTS synthesis (sent back from the server), and
    // highlights word-PAIRS within each sentence — proportioned by
    // each pair's share of that sentence's character count — so the
    // highlight moves roughly twice per sentence instead of once per
    // whole line, closer to natural reading pace.
    function updateHighlight(currentTime) {
        if (!highlightUnits || highlightUnits.length === 0) return;

        // Small linear scan is plenty fast for realistic transcript
        // lengths (a few hundred units at most).
        let activeIndex = -1;
        for (let i = 0; i < highlightUnits.length; i++) {
            if (currentTime >= highlightUnits[i].start && currentTime < highlightUnits[i].end) {
                activeIndex = i;
                break;
            }
        }
        // If between units (e.g. during a pause) or past the last
        // one, keep whichever unit's start time is closest to (and
        // not after) currentTime, so highlighting doesn't blank out
        // during the natural pauses between sentences.
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
    // Builds highlightUnits from the REAL per-sentence timings the
    // server measured while synthesizing audio. Falls back to the
    // old naive equal-split-by-line approach only if timings weren't
    // provided (e.g. an older cached response), so this never breaks
    // outright.
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

                // Group into pairs of 2 words each.
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
            // Fallback: no timing data available — split into lines
            // and spread them evenly across the (unknown, filled in
            // once metadata loads) audio duration, same as before.
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

    // NEW: click any word-pair in the transcript to jump the audio
    // straight to that point — uses the SAME real timing data the
    // highlight-follow logic uses, so clicking is exactly where the
    // highlight will land, not an approximation.
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
                // No real timing data (older cached response) — fall
                // back to the old equal-share estimate.
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
            // FIX (arrows disappeared): switching to theme:'base' meant
            // arrows/lines lost their default styling — 'base' needs a
            // FULL set of theme variables supplied manually or several
            // elements (like arrowheads) render invisible against the
            // dark background. Reverted to 'dark' (fully-styled,
            // working arrows/lines by default) and layered the visual
            // polish (rounded corners, shadows, curves) on top via
            // themeCSS instead, which works with any base theme.
            theme: 'dark',
            securityLevel: 'loose',
            fontFamily: 'Plus Jakarta Sans, sans-serif',
            flowchart: {
                htmlLabels: false,
                nodeSpacing: 35,
                rankSpacing: 90,
                padding: 48,
                curve: 'basis' // smooth curved connectors instead of sharp angles
            },
            themeVariables: {
                fontSize: '19px',
                // FIX: arrows were using the theme's default line color,
                // which happened to look the same green as the root
                // node — confusing since arrows aren't meant to signal
                // anything about branch color. A neutral, distinct
                // color (matching the arrowhead fix below) makes
                // arrows read as plain connectors regardless of which
                // branch colors are in play.
                lineColor: '#8b6fd6'
            },
            // Polish pass — rounded node corners + soft glow shadow
            // (matches the app's glass-card aesthetic elsewhere) and
            // thicker connector lines. Mermaid embeds this raw CSS
            // directly into the rendered SVG.
            themeCSS: `
                .node rect, .node polygon, .node circle, .node ellipse {
                    rx: 14px; ry: 14px;
                    filter: drop-shadow(0 4px 14px rgba(107, 48, 255, 0.28));
                }
                .edgePaths .path, .edgePath .path {
                    stroke-width: 2.5px;
                }
                /* FIX (arrows still green despite lineColor variable):
                   the theme variable wasn't winning against Mermaid's
                   own generated edge styles. Forcing the stroke color
                   directly on every class name Mermaid has used for
                   edge paths across versions, with !important, so it
                   can't lose to anything else. */
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
                /* FIX (arrowheads invisible): the connector LINES were
                   visible but the triangular arrowhead markers at each
                   line's end weren't — their fill wasn't being set
                   explicitly by our custom styling, so it fell back to
                   a color too close to the dark background to see.
                   Marker paths live inside <marker> defs referenced by
                   marker-end, and Mermaid gives them classes like
                   flowchart-pointEnd / arrowheadPath — style both to
                   be safe across Mermaid versions. */
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

    // FIX (arrows stayed green no matter what CSS was tried): Mermaid's
    // own generated styles for edges apparently win over any CSS rule
    // we inject, even with !important — likely due to how/where
    // themeCSS gets inserted relative to Mermaid's own <style> block.
    // Setting the color directly via JavaScript on each path element,
    // AFTER Mermaid has already rendered and styled everything, always
    // wins — it's not part of the CSS cascade at all.
    function forceEdgeColor(containerEl) {
        if (!containerEl) return;
        const svgEl = containerEl.querySelector('svg');
        if (!svgEl) return;
        // Every <path> that's an edge/connector (not inside a marker
        // definition, which holds the arrowhead triangle shapes).
        // Clearing any existing stroke attribute/inline style FIRST,
        // then setting both the attribute AND the inline style with
        // !important, covers every way Mermaid might have originally
        // colored it (SVG presentation attribute vs CSS vs inline
        // style all have different precedence — this beats all three).
        //
        // FIX (arrows looked like thick green "leaf/wing" shapes, not
        // thin lines): the edge paths had FILL set (not just stroke),
        // which is what created that tapered filled-shape look — our
        // earlier fix only forced stroke, so the fill stayed green and
        // kept the wing shape. Setting fill:none turns it back into a
        // normal thin stroked line.
        svgEl.querySelectorAll('path, line, polyline').forEach(p => {
            if (p.closest('marker')) return; // leave arrowhead shapes alone
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

    // FIX (wide diagrams got cut off at the sides): a fixed "always
    // zoom to 400%" default was too aggressive for diagrams with many
    // parallel branches — they overflowed the modal's visible width
    // badly. Auto-fit instead: measure the diagram's actual rendered
    // size and compute a scale that fits it within the modal's visible
    // area (capped so small diagrams don't get absurdly huge either).
    let mindmapZoom = 1;
    let mindmapNaturalWidth = 0;
    let mindmapNaturalHeight = 0;

    // FIX (couldn't scroll to see content past the left/top edge once
    // zoomed in): CSS transform:scale() only changes how something is
    // PAINTED, not its actual layout box size — so a parent with
    // overflow:auto never grows its scrollable area to match the
    // visually-bigger content, no matter how zoomed in it looks.
    // Setting real width/height (which the SVG then stretches to
    // fill, since it has a viewBox) makes the container's actual
    // layout size grow with zoom, so scrolling correctly reaches
    // every part of the zoomed-in diagram.
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
        mindmapZoomWrapper.style.transform = ''; // clear any old scale-based sizing
        mindmapModalBackdrop.classList.remove('hidden');

        // Measure after the modal is actually visible/laid out, then
        // pick a zoom level that fits the diagram to the viewport
        // (falls back to 100% if measurement isn't possible).
        requestAnimationFrame(() => {
            const svgEl = mindmapZoomWrapper.querySelector('svg');
            if (svgEl && mindmapModalBody) {
                // Measure the SVG's OWN natural size (its own width/
                // height, not the wrapper's, since the wrapper hasn't
                // been explicitly sized yet at this point) so later
                // zoom changes always scale from the same baseline.
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
                        2.5 // don't over-zoom small/simple diagrams either
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

        // FIX (color/fill fixes never showed up in the exported PDF no
        // matter how they were applied to the live on-screen DOM):
        // html2canvas apparently doesn't reflect JS-applied style/attr
        // changes made to a separately-inserted copy of the SVG. Doing
        // it HERE instead — directly on this parsed, string-based copy,
        // serialized back to text with XMLSerializer below — bakes the
        // fix directly into the markup TEXT itself, before it's ever
        // inserted into any DOM at all. That removes any ambiguity
        // about what html2canvas does or doesn't pick up live.
        svgEl.querySelectorAll('path, line, polyline').forEach(p => {
            if (p.closest('marker')) return; // leave arrowhead triangle shapes alone
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

        // Safety margin so edge content (text or thin connector lines)
        // never gets clipped — a moderate margin is enough now that
        // arrows are thin lines rather than filled wing shapes.
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

    // Shared by both PDF and PNG export — renders the mind map to an
    // off-screen canvas (cloneNode-based fix for colors, brute-force
    // padding for clipping — see history below). Returns the canvas,
    // or null (after showing an error banner) if it couldn't render.
    async function generateMindMapCanvas() {
        if (!lastMindMapSvg) {
            showErrorBanner('Download කරන්න Mind Map එකක් නෑ.');
            return null;
        }

        try {
            // FIX (arrows kept showing Mermaid's original green no
            // matter what): Mermaid embeds its OWN <style> block
            // INSIDE the SVG markup (with rules like ".edgePath
            // path { stroke: green !important }"). Cloning the LIVE,
            // already-correctly-styled DOM node with cloneNode(true)
            // (rather than re-serializing to a string) preserves our
            // forceEdgeColor() inline-style fix instead of losing it.
            const liveSvg = mindmapContainer.querySelector('svg');
            if (!liveSvg) {
                showErrorBanner('Mind Map එක load වී නොමැත.');
                return null;
            }
            const svgClone = liveSvg.cloneNode(true);

            // Re-apply the color fix directly on the clone too, as
            // a safety net (harmless if already correct).
            svgClone.querySelectorAll('path, line, polyline').forEach(p => {
                if (p.closest('marker')) return;
                p.setAttribute('stroke', '#8b6fd6');
                p.setAttribute('fill', 'none');
                p.setAttribute('stroke-width', '2.5');
                p.style.setProperty('stroke', '#8b6fd6', 'important');
                p.style.setProperty('fill', 'none', 'important');
            });

            // Work out the real size from the clone's own viewBox/
            // width/height, then add a safety margin.
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

            // Give the SVG an explicit background rect so it's not
            // transparent once rasterized (canvas has no CSS
            // background to fall back on).
            const bgRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            bgRect.setAttribute('x', String(vx - brutePadding));
            bgRect.setAttribute('y', String(vy - brutePadding));
            bgRect.setAttribute('width', String(width + brutePadding * 2));
            bgRect.setAttribute('height', String(height + brutePadding * 2));
            bgRect.setAttribute('fill', '#14141e');
            svgClone.insertBefore(bgRect, svgClone.firstChild);

            // FIX (arrows were missing entirely on mobile PNG
            // downloads, and quality was inconsistent): html2canvas
            // re-implements its own approximation of SVG rendering,
            // and that approximation behaves differently across
            // browser engines — desktop Firefox/Chrome handled our
            // edge paths fine, but mobile browsers' html2canvas code
            // path apparently dropped them. Switching to the browser's
            // OWN native SVG renderer instead — load the serialized
            // SVG into a real <img>, then draw that onto a canvas —
            // sidesteps html2canvas's SVG handling entirely and uses
            // the exact same rendering engine that already draws the
            // mind map correctly on screen, on every platform.
            // FIX ("Tainted canvases may not be exported" on mobile):
            // the SVG's text uses font-family: 'Plus Jakarta Sans'
            // (loaded from Google Fonts at the PAGE level). When that
            // same SVG is rendered stand-alone via a blob URL + <img>,
            // some mobile browsers try to fetch that external font to
            // render the text and treat the canvas as tainted by
            // cross-origin content afterward — even though the SVG
            // itself came from a same-origin blob. Swapping to a
            // built-in system font here removes any external
            // resource dependency during the render, avoiding the
            // taint entirely. (On-screen display is unaffected — this
            // only touches this exported copy.)
            let svgString = new XMLSerializer().serializeToString(svgClone);
            svgString = svgString.replace(/Plus Jakarta Sans,?\s*/g, '');

            // FIX (still tainted on mobile even after stripping the
            // external font): blob: URLs are apparently still treated
            // as "foreign" enough by some mobile browsers' canvas
            // security checks to taint the canvas once drawn. A
            // base64 data: URI is fully self-contained inline data
            // with no indirection at all — this is the more
            // consistently reliable fix documented for this exact
            // "tainted canvas" issue with SVG-to-canvas rendering.
            const svgBase64 = btoa(unescape(encodeURIComponent(svgString)));
            const svgDataUrl = `data:image/svg+xml;base64,${svgBase64}`;

            const img = new Image();
            await new Promise((resolve, reject) => {
                img.onload = resolve;
                img.onerror = reject;
                img.src = svgDataUrl;
            });

            // Adaptive scale: stay under a safe max canvas dimension
            // so mobile browsers (which often silently cap canvas
            // size around 4096px per side) never hit that ceiling —
            // past it, browsers quietly downsample/clip instead of
            // erroring, which is what looked like "quality loss".
            const SAFE_MAX_CANVAS_DIMENSION = 4000;
            const desiredScale = 2.5;
            const largestSide = Math.max(finalWidth, finalHeight);
            const adaptiveScale = Math.min(desiredScale, SAFE_MAX_CANVAS_DIMENSION / largestSide);

            const canvas = document.createElement('canvas');
            canvas.width = Math.round(finalWidth * adaptiveScale);
            canvas.height = Math.round(finalHeight * adaptiveScale);
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

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

    // NEW: PNG export — same rendering pipeline as PDF, but downloads
    // the raw image directly instead of wrapping it in a PDF page.
    // Handy for pasting into a document/presentation, or when a
    // plain image is more convenient to share than a PDF.
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

    function closeUploadSourceMenu() {
        if (uploadSourceMenu) uploadSourceMenu.classList.add('hidden');
    }

    function toggleUploadSourceMenu() {
        if (uploadSourceMenu) uploadSourceMenu.classList.toggle('hidden');
    }

    // NEW: clicking the upload area no longer jumps straight into the
    // OS's plain file picker — it shows a small "Gallery / Camera"
    // choice menu instead. This is more reliable than depending on
    // browsers to surface a camera option in their native picker
    // (behavior for a bare <input type="file"> without "capture"
    // varies a lot and wasn't consistently offering a camera choice),
    // and it needs no separate always-visible camera button on the
    // page — the choice only appears right when it's needed.
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

    // Close the menu if the person clicks/taps anywhere else on the page.
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
        const files = e.dataTransfer.files;
        if (files.length > 0 && !isOCRRunning) {
            handleImage(files[0]);
        }
    });

    imageInput.addEventListener('change', function(e) {
        if (this.files.length > 0 && !isOCRRunning) {
            handleImage(this.files[0]);
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
                body: JSON.stringify({ text, mode }),
            });

            const data = await response.json();

            if (data.status === 'success') {
                scriptOutput.value = data.processed_text;
                safetySection.classList.remove('hidden');
                audioSection.classList.add('hidden');
                trackUsageEvent('note_processed');

                if (data.ai_processed === false && data.warning) {
                    showErrorBanner(data.warning);
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

        generateAudioBtn.innerHTML = '<span class="mini-wave"><span></span><span></span><span></span><span></span></span> Generating...';
        generateAudioBtn.disabled = true;

        try {
            const response = await fetch('/tts', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text }),
            });

            const data = await response.json();

            if (data.status === 'success') {
                audioSection.classList.remove('hidden');
                renderLyrics(text, data.sentence_timings);
                trackUsageEvent('audio_generated');

                if (audio) {
                    audio.pause();
                    audio = null;
                }

                audio = new Audio(data.audio_url);
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

                progressBar.addEventListener('click', function(e) {
                    if (!audio || !audio.duration || isNaN(audio.duration) || !isFinite(audio.duration)) return;

                    const rect = this.getBoundingClientRect();
                    const x = e.clientX - rect.left;
                    const percentage = Math.min(1, Math.max(0, x / rect.width));
                    const seekTime = percentage * audio.duration;

                    audio.currentTime = seekTime;
                    currentTimeEl.textContent = formatTime(seekTime);
                    updateProgress(percentage * 100);
                    updateHighlight(seekTime);
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

    console.log('🎵 NoteWav AI Loaded — Mind Maps + gTTS narration ready!');
});

// ========================================
// SPLASH SCREEN: guaranteed hide (JS fallback)
// ========================================
// FIX (splash screen stuck permanently, page never showing): the CSS
// animation-delay + forwards approach can silently fail in several
// real-world cases — a backgrounded tab throttling/pausing the
// animation timer, prefers-reduced-motion rules interacting oddly
// with the delay, or the animation simply never being triggered. This
// forces the splash screen away after a fixed timeout regardless of
// what the CSS animation is doing, so the app can never get stuck
// behind it.
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
if ('serviceWorker' in navigator) {
    window.addEventListener('load', function() {
        navigator.serviceWorker.register('/static/sw.js')
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
// SAFETY-CHECK TEXT FONT SIZE ADJUSTMENT
// ========================================
// Lets students bump the script-output textarea's font size up/down
// for easier reading, persisted across visits via localStorage.
document.addEventListener('DOMContentLoaded', function() {
    const FONT_SIZE_KEY = 'notewav_script_font_size';
    const MIN_PERCENT = 70;
    const MAX_PERCENT = 160;
    const STEP = 10;
    const BASE_PX = 16; // the textarea's default font-size in px

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
// Tracks consecutive-day usage entirely client-side (localStorage) —
// no server/database needed. Visiting on a new calendar day that's
// exactly one day after the last visit increments the streak; a
// skipped day resets it back to 1. Now shown in the menu drawer
// (moved out of the header to make room for the coins badge).
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
    } catch (e) {
        console.warn('Study streak tracking unavailable:', e);
    }
});

// ========================================
// APP UI LANGUAGE (Sinhala / English toggle)
// ========================================
// Translates the app's own STATIC labels/buttons (not AI-generated
// content like the podcast script or mind map, which already follow
// the note's own language). Sinhala is the default/current style;
// English is a full translation of every tagged string. Persisted in
// localStorage so the choice survives across visits.
const NOTEWAV_TRANSLATIONS = {
    my_library: { si: 'My Library', en: 'My Library' },
    settings_title: { si: 'සැකසුම්', en: 'Settings' },
    menu_title: { si: 'මෙනුව (Menu)', en: 'Menu' },
    settings_language_label: { si: 'App භාෂාව (Language)', en: 'App Language' },
    library_search_placeholder: { si: 'Title/Subject එකෙන් හොයන්න...', en: 'Search by Title/Subject...' },
    header_tagline: { si: 'Smart විදිහට පාඩම් අහන්න. NoteWav AI', en: 'Listen to your notes, the smart way. NoteWav AI' },
    card1_title: { si: 'ඔබේ සටහන ඇතුළත් කරන්න', en: 'Enter Your Note' },
    ocr_section_title: { si: 'රූපයක් මඟින් කරුණු ලබාදෙන්න (OCR):', en: 'Provide content via an image (OCR):' },
    upload_area_text: { si: 'පාඩමේ රූපයක් මෙතනට ඇද දමන්න හෝ ක්ලික් කරන්න', en: 'Drag a photo of the lesson here or click to browse' },
    upload_hint: { si: 'PNG, JPG, JPEG, WEBP ගොනු පමණයි (Max 5MB)', en: 'PNG, JPG, JPEG, WEBP files only (Max 5MB)' },
    ocr_status_text: { si: 'පෙළ උපුටා ගැනීම සිදුවෙමින්...', en: 'Extracting text...' },
    note_input_placeholder: { si: 'මෙතනට ඔබේ සටහන් ඇතුළත් කරන්න. නැතිනම් කැමති ප්‍රශ්නයක් අහන්න...', en: 'Type your notes here, or ask any question you like...' },
    char_count_suffix: { si: '/ 2000 අක්ෂර', en: '/ 2000 characters' },
    study_mode_title: { si: 'අධ්‍යාපනික මාදිලිය (Study Mode):', en: 'Study Mode:' },
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
// No login system exists yet — this is purely a device-local nicety
// (like the streak tracker and font-size preference) so the person
// can personalize their own view of the app: a display name and a
// small profile photo, both saved in localStorage. Images are resized
// down to a small square via canvas before storing, since localStorage
// has limited space and a full-resolution photo would waste it.
document.addEventListener('DOMContentLoaded', function() {
    const PROFILE_KEY = 'notewav_profile';
    const avatarBtn = document.getElementById('profile-avatar-btn');
    const avatarImg = document.getElementById('profile-avatar-img');
    const avatarPlaceholder = document.getElementById('profile-avatar-placeholder');
    const avatarInput = document.getElementById('profile-avatar-input');
    const nameInput = document.getElementById('profile-name-input');
    if (!avatarBtn || !avatarImg || !avatarInput || !nameInput) return;

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
    }

    function saveProfile(partial) {
        try {
            const current = JSON.parse(localStorage.getItem(PROFILE_KEY) || '{}');
            const updated = Object.assign({}, current, partial);
            localStorage.setItem(PROFILE_KEY, JSON.stringify(updated));
        } catch (e) {
            console.warn('Could not save profile:', e);
        }
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

    nameInput.addEventListener('input', function() {
        saveProfile({ name: this.value });
    });

    loadProfile();
});

// ========================================
// TIME-BASED GREETING (uses saved profile name)
// ========================================
// Exposed as a standalone global function (not just DOMContentLoaded-
// scoped) so applyAppLanguage() can call it directly whenever the
// language toggle changes — otherwise the greeting would only ever
// reflect whatever language was active at the moment the page first
// loaded, staying stale after a later language switch.
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
// Shows only when no name has been saved yet AND the person hasn't
// already dismissed/skipped it before — so it never nags on repeat
// visits, whether they filled it in or chose to skip.
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
        // if storage is unreadable, just don't show the card rather
        // than risk nagging every visit
        onboardingDone = true;
    }

    if (existingName || onboardingDone) {
        return; // already have a name, or the person already dismissed this once
    }

    // Small delay so it appears after the splash screen fades, not
    // stacked on top of it.
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
                localStorage.setItem(PROFILE_KEY, JSON.stringify(current));
            } catch (e) {
                console.warn('Could not save name:', e);
            }
            // Keep the menu drawer's profile field in sync too, in
            // case the person opens it later in this same session.
            const drawerNameInput = document.getElementById('profile-name-input');
            if (drawerNameInput) drawerNameInput.value = name;
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
// Accumulates real listening time via the natural small deltas
// between consecutive 'timeupdate' events during normal playback —
// deliberately ignores big jumps (seeks, skip buttons, new audio
// loads) so a single skip doesn't get miscounted as minutes studied.
// Resets each new calendar day, stored in localStorage like the
// streak tracker.
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
// Uses the browser's built-in Web Speech API — free, no server cost,
// but support/quality varies by browser and language (works best in
// Chrome; Sinhala recognition quality depends entirely on Google's
// speech backend behind the scenes, so results may be imperfect —
// treat this as a rough starting point students can then edit, not a
// guaranteed-accurate transcription).
document.addEventListener('DOMContentLoaded', function() {
    const voiceBtn = document.getElementById('note-input-voice-btn');
    const targetNoteInput = document.getElementById('note-input');
    if (!voiceBtn || !targetNoteInput) return;

    const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognitionCtor) {
        voiceBtn.style.display = 'none'; // hide entirely if unsupported, rather than showing a dead button
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
            voiceBtn.classList.add('copied'); // reuse the existing "active" green style
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
// The notes database isn't guaranteed to survive a redeploy on
// Render's free tier — this lets a student download all their saved
// notes as a single JSON file (real backup, kept on their own device)
// and re-import it later if the server-side data is ever lost.
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
// NoteWav doesn't have any paid/coin-consuming features yet — this is
// a forward-looking placeholder so the UI/UX for a future "coins"
// economy already exists. Everyone currently gets a starting balance
// of FREE coins (clearly labelled as such, not implying real currency
// or a purchase). When real paid features are added later, hook into
// spendCoins()/addCoins() here rather than building a new system from
// scratch.
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
    if (el) el.textContent = String(getCoinsBalance());
}

// Exposed globally so a future feature can call these directly, e.g.
// spendCoins(10) before unlocking something paid.
function spendCoins(amount) {
    const current = getCoinsBalance();
    if (current < amount) return false;
    try {
        localStorage.setItem(COINS_KEY, String(current - amount));
    } catch (e) {
        console.warn('Could not update coins balance:', e);
    }
    updateCoinsDisplay();
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
    const privacyLink = document.getElementById('privacy-link');
    if (aboutLink) aboutLink.addEventListener('click', e => { e.preventDefault(); openInfoModal('about'); });
    if (supportLink) supportLink.addEventListener('click', e => { e.preventDefault(); openInfoModal('support'); });
    if (privacyLink) privacyLink.addEventListener('click', e => { e.preventDefault(); openInfoModal('privacy'); });

    if (infoModalClose) infoModalClose.addEventListener('click', () => infoModalBackdrop.classList.add('hidden'));
    infoModalBackdrop.addEventListener('click', function(e) {
        if (e.target === infoModalBackdrop) infoModalBackdrop.classList.add('hidden');
    });
});

// ========================================
// NOTIFICATION BELL (admin → users broadcast)
// ========================================
// Polls /announcements/latest periodically. If the latest
// announcement's ID is newer than whatever ID this browser last
// marked as "seen" (localStorage), a red dot badge appears on the
// bell. Clicking the bell shows the message and marks it seen,
// clearing the badge — same pattern as any simple notification
// system, just without needing real user accounts.
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
            const res = await fetch('/announcements/latest');
            const data = await res.json();
            if (data.status !== 'success' || !data.announcement) return;
            latestAnnouncement = data.announcement;
            if (latestAnnouncement.id > getSeenId()) {
                badge.classList.remove('hidden');
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
    setInterval(checkForAnnouncement, 30000); // check every 30 seconds
});