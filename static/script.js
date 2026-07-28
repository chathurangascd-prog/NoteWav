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
    let lyricsLines = [];
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
            noteInput.style.height = 'auto';
            noteInput.style.height = noteInput.scrollHeight + 'px';

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
    function updateHighlight(currentTime) {
        if (!lyricsLines || lyricsLines.length === 0) return;

        const totalDuration = audio.duration || 1;
        const lineCount = lyricsLines.length;
        const timePerLine = totalDuration / lineCount;

        let currentLineIndex = Math.floor(currentTime / timePerLine);
        if (currentLineIndex >= lineCount) {
            currentLineIndex = lineCount - 1;
        }

        const lineElements = highlightContainer.querySelectorAll('.lyric-line');
        lineElements.forEach(el => el.classList.remove('active'));

        if (lineElements[currentLineIndex]) {
            lineElements[currentLineIndex].classList.add('active');
            lineElements[currentLineIndex].scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }

    // ========================================
    // RENDER LYRICS WITH LINE-BY-LINE FORMAT
    // ========================================
    function renderLyrics(text) {
        if (!text) {
            highlightContainer.innerHTML = '';
            return;
        }

        lyricsLines = splitTextIntoLines(text);

        if (lyricsLines.length === 0) {
            highlightContainer.innerHTML = `<p>${text}</p>`;
            return;
        }

        let html = '';
        lyricsLines.forEach((line, index) => {
            html += `<span class="lyric-line" data-index="${index}">${line}</span>`;
            if (index < lyricsLines.length - 1) {
                html += ' ';
            }
        });

        highlightContainer.innerHTML = html;
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
                nodeSpacing: 70,
                rankSpacing: 85,
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

    function setMindMapZoom(level) {
        mindmapZoom = Math.min(4, Math.max(0.3, level));
        if (mindmapZoomWrapper) mindmapZoomWrapper.style.transform = `scale(${mindmapZoom})`;
        if (mindmapZoomLevelEl) mindmapZoomLevelEl.textContent = `${Math.round(mindmapZoom * 100)}%`;
    }

    function openMindMapModal() {
        if (!lastMindMapSvg || !mindmapModalBackdrop || !mindmapZoomWrapper) {
            showErrorBanner('විශාල කර බලන්න Mind Map එකක් නෑ.');
            return;
        }
        mindmapZoomWrapper.innerHTML = lastMindMapSvg;
        forceEdgeColor(mindmapZoomWrapper);
        mindmapModalBackdrop.classList.remove('hidden');

        // Measure after the modal is actually visible/laid out, then
        // pick a zoom level that fits the diagram to the viewport
        // (falls back to 100% if measurement isn't possible).
        requestAnimationFrame(() => {
            const svgEl = mindmapZoomWrapper.querySelector('svg');
            if (svgEl && mindmapModalBody) {
                const svgRect = svgEl.getBoundingClientRect();
                const margin = 70;
                const availableWidth = mindmapModalBody.clientWidth - margin;
                const availableHeight = mindmapModalBody.clientHeight - margin;
                if (svgRect.width > 0 && svgRect.height > 0) {
                    const fitScale = Math.min(
                        availableWidth / svgRect.width,
                        availableHeight / svgRect.height,
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

        let tempContainer = null;
        try {
            // FIX (arrows kept showing Mermaid's original green no
            // matter what): Mermaid embeds its OWN <style> block
            // INSIDE the SVG markup (with rules like ".edgePath
            // path { stroke: green !important }"). ANY approach
            // that goes through a STRING at some point — innerHTML,
            // outerHTML, or DOMParser+XMLSerializer — carries that
            // embedded stylesheet along with it, and its !important
            // rules keep beating our attribute/inline-style changes
            // every time we re-insert that markup. The fix: never
            // re-serialize to a string at all. mindmapContainer's
            // SVG (the small, un-enlarged preview) is already
            // confirmed correct on screen — clone that LIVE DOM
            // NODE directly with cloneNode(true), which copies its
            // current attributes/inline-styles as real DOM state,
            // not by re-parsing text. That preserves our earlier
            // forceEdgeColor() fix (also inline-style, so it has
            // equal-or-higher priority than the embedded
            // stylesheet) instead of losing it.
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
            // width/height (same logic as before, just without a
            // string round-trip), then add a safety margin.
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

            tempContainer = document.createElement('div');
            tempContainer.style.position = 'fixed';
            tempContainer.style.left = '-99999px';
            tempContainer.style.top = '0';
            tempContainer.style.width = finalWidth + 'px';
            tempContainer.style.height = finalHeight + 'px';
            tempContainer.style.background = '#14141e';
            tempContainer.appendChild(svgClone); // appendChild the actual node — not innerHTML with a string
            document.body.appendChild(tempContainer);

            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));

            const html2canvasModule = await import('https://esm.sh/html2canvas@1.4.1');
            const html2canvas = html2canvasModule.default;

            const canvas = await html2canvas(tempContainer, {
                backgroundColor: '#14141e',
                scale: 2.5,
                width: finalWidth,
                height: finalHeight,
                logging: false,
            });

            document.body.removeChild(tempContainer);
            tempContainer = null;
            return canvas;
        } catch (err) {
            if (tempContainer && tempContainer.parentNode) document.body.removeChild(tempContainer);
            console.error('Mind map canvas render error:', err);
            showErrorBanner('Mind map render කරගැනීම අසාර්ථක විය: ' + (err && err.message ? err.message : err));
            return null;
        }
    }

    if (mindmapDownloadPdfBtn) {
        mindmapDownloadPdfBtn.addEventListener('click', async function() {
            const canvas = await generateMindMapCanvas();
            if (!canvas) return;

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
                noteInput.style.height = 'auto';
                noteInput.style.height = noteInput.scrollHeight + 'px';

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
                    lyricsLines = [];
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
                renderLyrics(text);

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
            lyricsLines = [];
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
            this.style.height = 'auto';
            this.style.height = this.scrollHeight + 'px';
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
// skipped day resets it back to 1.
document.addEventListener('DOMContentLoaded', function() {
    const STREAK_KEY = 'notewav_streak_data';
    const streakBadge = document.getElementById('streak-badge');
    const streakCountEl = document.getElementById('streak-count');
    if (!streakBadge || !streakCountEl) return;

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
        streakCountEl.textContent = String(streak);
        streakBadge.classList.remove('hidden');
    } catch (e) {
        console.warn('Study streak tracking unavailable:', e);
    }
});