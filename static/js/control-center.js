const TURN_HOST = 'turn.ayux.in';
const TURN_PORT = 3478;
const TURN_USERNAME = 'cctv';
const TURN_PASSWORD = 'wheresrusty';

const TURN_SERVERS = [
    { urls: `turn:${TURN_HOST}:${TURN_PORT}`, username: TURN_USERNAME, credential: TURN_PASSWORD },
    { urls: `turn:${TURN_HOST}:${TURN_PORT}?transport=tcp`, username: TURN_USERNAME, credential: TURN_PASSWORD }
];

const socket = io(window.location.origin, { path: '/socket.io' });
const SECURE_MEDIA_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);
const requiresSecureMediaContext = location.protocol !== 'https:' && !SECURE_MEDIA_HOSTS.has(location.hostname);
let onlineUsersPollTimer = null;

const viewerCounts = {};
let knownCameras = [];
const localVideoEl = document.getElementById('localVideo');
const broadcastModeSelect = document.getElementById('broadcastMode');
const modeHelpTextEl = document.getElementById('modeHelpText');
const motionPanelEl = document.getElementById('motionPanel');
const motionStatusEl = document.getElementById('motionStatus');
const motionClipsEl = document.getElementById('motionClips');

const broadcastState = {
    active: false,
    cameraId: null,
    stream: null,
    heartbeatTimer: null,
    viewerPollTimer: null,
    statusEl: document.getElementById('bcStatus'),
    buttonEl: document.getElementById('startBtn'),
    viewerCountEl: document.getElementById('viewerCountValue'),
    micBtn: document.getElementById('broadcastMicBtn'),
    videoBtn: document.getElementById('broadcastVideoBtn'),
    mediaControlsEl: document.getElementById('broadcastMediaControls'),
    audioEnabled: true,
    videoEnabled: true,
    mode: broadcastModeSelect.value,
    settings: null,
    pendingStreamPromise: null,
    lastViewerCount: 0
};
broadcastState.buttonEl.onclick = startBroadcast;

const broadcastPeerConnections = {};

const wakeLockManager = (() => {
    let sentinel = null;
    let enabled = false;

    async function requestLock() {
        if (!('wakeLock' in navigator) || !enabled || document.visibilityState !== 'visible') return;
        try {
            sentinel = await navigator.wakeLock.request('screen');
            sentinel.addEventListener('release', () => {
                sentinel = null;
                if (enabled) {
                    requestLock();
                }
            });
        } catch (err) {
            console.warn('Wake lock failed', err);
        }
    }

    document.addEventListener('visibilitychange', () => {
        if (enabled && !sentinel && document.visibilityState === 'visible') {
            requestLock();
        }
    });

    return {
        async enable() {
            enabled = true;
            if (!sentinel) {
                await requestLock();
            }
        },
        async disable() {
            enabled = false;
            if (sentinel) {
                try {
                    await sentinel.release();
                } catch (err) {
                    console.warn('Wake lock release failed', err);
                }
                sentinel = null;
            }
        }
    };
})();

const motionRecorder = {
    canvas: document.createElement('canvas'),
    ctx: null,
    rafId: null,
    lastFrame: null,
    recorder: null,
    recording: false,
    chunks: [],
    idleTimer: null,
    threshold: 30,
    ratio: 0.015,
    cooldownMs: 8000,
    active: false
};
motionRecorder.statusEl = motionStatusEl;
motionRecorder.listEl = motionClipsEl;

function updateModeHelpText() {
    if (!modeHelpTextEl) return;
    if (broadcastState.mode === 'viewer-triggered') {
        modeHelpTextEl.textContent = 'Camera sleeps until someone tunes in. Screen stays awake.';
    } else {
        modeHelpTextEl.textContent = 'Camera runs nonstop and saves motion clips to the server.';
    }
}

function toggleMotionPanel() {
    if (!motionPanelEl) return;
    const shouldShow = broadcastState.mode === 'always-on';
    motionPanelEl.style.display = shouldShow ? 'block' : 'none';
    if (!shouldShow) {
        stopMotionDetection();
    } else if (broadcastState.stream) {
        startMotionDetection();
    }
}

function appendMotionClip(url, timestamp, bytes, cameraLabel) {
    if (!motionRecorder.listEl) return;
    if (motionRecorder.listEl.firstElementChild && motionRecorder.listEl.firstElementChild.classList.contains('mode-help')) {
        motionRecorder.listEl.innerHTML = '';
    }
    const item = document.createElement('div');
    item.className = 'recording-item';
    const timeLabel = new Date(timestamp).toLocaleString();
    const sizeLabel = bytes ? `${Math.max(1, Math.round(bytes / 1024))} KB` : '';
    item.innerHTML = `
        <div>
            <div class="recording-title">${timeLabel}</div>
            <div class="recording-meta">${cameraLabel || broadcastState.cameraId || 'camera'}${sizeLabel ? ' · ' + sizeLabel : ''}</div>
        </div>
        <a class="recording-download" href="${url}" target="_blank">Download</a>
    `;
    motionRecorder.listEl.prepend(item);
    while (motionRecorder.listEl.childElementCount > 5) {
        motionRecorder.listEl.lastElementChild?.remove();
    }
}

async function ensureBroadcastStream(reason = '') {
    if (broadcastState.stream) return broadcastState.stream;
    if (broadcastState.pendingStreamPromise) return broadcastState.pendingStreamPromise;

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        setStatus(broadcastState.statusEl, 'Camera capture is not supported in this browser.', 'error');
        throw new Error('getUserMedia unsupported');
    }

    const settings = broadcastState.settings || {
        width: 640,
        height: 480,
        audio: true
    };
    const constraints = {
        video: { width: { ideal: settings.width }, height: { ideal: settings.height } },
        audio: settings.audio
    };

    const request = navigator.mediaDevices.getUserMedia(constraints)
        .then(stream => {
            if (!broadcastState.active) {
                stream.getTracks().forEach(track => track.stop());
                throw new Error('Broadcast stopped');
            }
            broadcastState.stream = stream;
            localVideoEl.srcObject = stream;
            broadcastState.mediaControlsEl.style.display = 'flex';
            broadcastState.audioEnabled = settings.audio;
            broadcastState.videoEnabled = true;
            updateBroadcastMediaButtons();
            if (reason) {
                setStatus(broadcastState.statusEl, `Camera live — ${reason}`, 'success');
            }
            if (broadcastState.mode === 'always-on') {
                startMotionDetection();
            }
            return stream;
        })
        .catch(err => {
            setStatus(broadcastState.statusEl, err?.message || 'Failed to access camera', 'error');
            throw err;
        })
        .finally(() => {
            broadcastState.pendingStreamPromise = null;
        });

    broadcastState.pendingStreamPromise = request;
    return request;
}

function releaseBroadcastStream(reason = '') {
    if (broadcastState.pendingStreamPromise) {
        // Let the pending request finish; it will no-op if stopped meanwhile.
    }
    if (!broadcastState.stream) return;
    stopMotionDetection();
    broadcastState.stream.getTracks().forEach(track => track.stop());
    broadcastState.stream = null;
    localVideoEl.srcObject = null;
    broadcastState.mediaControlsEl.style.display = 'none';
    if (reason) {
        setStatus(broadcastState.statusEl, `Camera idle — ${reason}`, 'info');
    }
}

function startMotionDetection() {
    if (broadcastState.mode !== 'always-on' || !broadcastState.stream) return;
    if (motionRecorder.active) return;
    motionRecorder.active = true;
    motionRecorder.ctx = motionRecorder.canvas.getContext('2d', { willReadFrequently: true });

    const primeAndLoop = () => {
        if (!localVideoEl.videoWidth || !localVideoEl.videoHeight) {
            motionRecorder.rafId = requestAnimationFrame(primeAndLoop);
            return;
        }
        motionRecorder.canvas.width = Math.min(320, localVideoEl.videoWidth || 320);
        motionRecorder.canvas.height = Math.min(180, localVideoEl.videoHeight || 180);
        motionRecorder.lastFrame = null;
        if (motionRecorder.statusEl) {
            motionRecorder.statusEl.style.display = 'block';
            setStatus(motionRecorder.statusEl, 'Monitoring for movement...', 'info');
        }
        motionRecorder.rafId = requestAnimationFrame(motionDetectionLoop);
    };

    primeAndLoop();
}

function stopMotionDetection() {
    if (!motionRecorder.active) return;
    motionRecorder.active = false;
    if (motionRecorder.rafId) {
        cancelAnimationFrame(motionRecorder.rafId);
        motionRecorder.rafId = null;
    }
    motionRecorder.lastFrame = null;
    if (motionRecorder.idleTimer) {
        clearTimeout(motionRecorder.idleTimer);
        motionRecorder.idleTimer = null;
    }
    stopMotionRecording();
    if (motionRecorder.statusEl) {
        motionRecorder.statusEl.style.display = 'none';
    }
}

function motionDetectionLoop() {
    if (!motionRecorder.active || !motionRecorder.ctx) return;
    const video = localVideoEl;
    if (!video || video.readyState < 2) {
        motionRecorder.rafId = requestAnimationFrame(motionDetectionLoop);
        return;
    }
    const width = motionRecorder.canvas.width || Math.min(320, video.videoWidth || 320);
    const height = motionRecorder.canvas.height || Math.min(180, video.videoHeight || 180);
    motionRecorder.canvas.width = width;
    motionRecorder.canvas.height = height;
    motionRecorder.ctx.drawImage(video, 0, 0, width, height);
    const frameData = motionRecorder.ctx.getImageData(0, 0, width, height).data;
    if (motionRecorder.lastFrame) {
        let motionPixels = 0;
        const length = frameData.length;
        for (let i = 0; i < length; i += 16) {
            const delta = Math.abs(frameData[i] - motionRecorder.lastFrame[i]) +
                Math.abs(frameData[i + 1] - motionRecorder.lastFrame[i + 1]) +
                Math.abs(frameData[i + 2] - motionRecorder.lastFrame[i + 2]);
            if (delta > motionRecorder.threshold) {
                motionPixels++;
            }
        }
        const ratio = motionPixels / Math.max(1, (frameData.length / 16));
        if (ratio > motionRecorder.ratio) {
            handleMotionDetected(ratio);
        } else {
            handleMotionCleared();
        }
    }
    motionRecorder.lastFrame = frameData.slice(0);
    motionRecorder.rafId = requestAnimationFrame(motionDetectionLoop);
}

function handleMotionDetected(ratio) {
    if (motionRecorder.statusEl) {
        setStatus(motionRecorder.statusEl, `Motion detected (${(ratio * 100).toFixed(1)}% frame)`, 'warn');
    }
    if (!motionRecorder.recording) {
        startMotionRecording();
    }
    if (motionRecorder.idleTimer) {
        clearTimeout(motionRecorder.idleTimer);
    }
    motionRecorder.idleTimer = setTimeout(() => {
        stopMotionRecording();
        if (motionRecorder.statusEl && !motionRecorder.recording) {
            setStatus(motionRecorder.statusEl, 'Monitoring for movement...', 'info');
        }
    }, motionRecorder.cooldownMs);
}

function handleMotionCleared() {
    if (motionRecorder.recording) return;
    if (motionRecorder.statusEl) {
        setStatus(motionRecorder.statusEl, 'Monitoring for movement...', 'info');
    }
}

function startMotionRecording() {
    if (!window.MediaRecorder || !broadcastState.stream) {
        setStatus(motionRecorder.statusEl, 'Motion detected but recording is not supported in this browser.', 'error');
        return;
    }
    if (motionRecorder.recording) return;
    try {
        motionRecorder.recorder = new MediaRecorder(broadcastState.stream, { mimeType: 'video/webm;codecs=vp8,opus' });
    } catch (err) {
        try {
            motionRecorder.recorder = new MediaRecorder(broadcastState.stream);
        } catch (inner) {
            console.warn('Unable to start MediaRecorder', inner);
            setStatus(motionRecorder.statusEl, inner?.message || 'Failed to start recorder', 'error');
            return;
        }
    }
    motionRecorder.chunks = [];
    motionRecorder.recorder.ondataavailable = (event) => {
        if (event.data && event.data.size) {
            motionRecorder.chunks.push(event.data);
        }
    };
    motionRecorder.recorder.onstop = () => {
        const blob = new Blob(motionRecorder.chunks, { type: 'video/webm' });
        motionRecorder.chunks = [];
        motionRecorder.recorder = null;
        if (blob.size) {
            uploadMotionClip(blob);
        }
    };
    motionRecorder.recording = true;
    motionRecorder.recorder.start();
    setStatus(motionRecorder.statusEl, 'Recording motion...', 'success');
}

function stopMotionRecording() {
    if (!motionRecorder.recording || !motionRecorder.recorder) return;
    motionRecorder.recording = false;
    try {
        motionRecorder.recorder.stop();
    } catch (err) {
        console.warn('Recorder stop failed', err);
    }
}

async function uploadMotionClip(blob) {
    if (!blob || !blob.size || !broadcastState.cameraId) return;
    const timestamp = new Date().toISOString();
    const form = new FormData();
    form.append('camera_id', broadcastState.cameraId);
    form.append('timestamp', timestamp);
    form.append('clip', blob, `${broadcastState.cameraId}-${timestamp}.webm`);
    try {
        const response = await fetch('/api/motion-clips', { method: 'POST', body: form });
        let payload = {};
        try {
            payload = await response.json();
        } catch (parseErr) {
            if (!response.ok) {
                throw new Error('Failed to save motion clip');
            }
            throw parseErr;
        }
        if (!response.ok || !payload.success) {
            throw new Error(payload?.error || 'Failed to save motion clip');
        }
        appendMotionClip(payload.clip_url, payload.timestamp, payload.bytes, payload.camera_id);
        setStatus(motionRecorder.statusEl, 'Motion clip saved', 'success');
    } catch (err) {
        console.error(err);
        setStatus(motionRecorder.statusEl, err?.message || 'Failed to save motion clip', 'error');
    }
}

const watchState = {
    peer: null,
    cameraId: null,
    remoteStream: null,
    retryTimer: null,
    manualClose: false
};

const callState = {
    username: null,
    autoPickup: true,
    callId: null,
    participants: [],
    peers: {},
    remoteStreams: {},
    pendingCallId: null,
    incomingCallId: null,
    localStream: null,
    localVideo: document.getElementById('callLocalVideo'),
    remoteGrid: document.getElementById('callRemoteGrid'),
    statusEl: document.getElementById('callStatusBanner'),
    presenceChip: document.getElementById('callPresenceChip'),
    incomingBanner: document.getElementById('incomingCallBanner'),
    incomingText: document.getElementById('incomingCallText'),
    acceptBtn: document.getElementById('acceptCallBtn'),
    declineBtn: document.getElementById('declineCallBtn'),
    leaveBtn: document.getElementById('leaveCallBtn'),
    participantsEl: document.getElementById('activeCallParticipants'),
    onlineUsersEl: document.getElementById('onlineUsersList'),
    helpEl: document.getElementById('callHelpText'),
    lastSeenUsers: [],
    micBtn: document.getElementById('callMicBtn'),
    videoBtn: document.getElementById('callVideoBtn'),
    muteIndicator: document.getElementById('callLocalMuteIndicator'),
    audioEnabled: true,
    videoEnabled: true
};

const callNameInput = document.getElementById('callNameInput');
const autoPickupToggle = document.getElementById('autoPickupToggle');
const saveCallProfileBtn = document.getElementById('saveCallProfileBtn');
const closeRemoteBtn = document.getElementById('closeRemoteBtn');

function switchTab(n) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
    document.getElementById('tab' + n).classList.add('active');
    document.querySelectorAll('.tab-btn')[n].classList.add('active');
}

function setStatus(el, text, variant = 'info') {
    if (!el) return;
    el.textContent = text;
    el.style.display = 'block';
    el.classList.remove('info', 'success', 'warn', 'error');
    el.classList.add(variant);
}

function startOnlineUsersPolling() {
    if (onlineUsersPollTimer) return;
    onlineUsersPollTimer = setInterval(() => {
        socket.emit('request_online_users');
    }, 10000);
}

function stopOnlineUsersPolling() {
    if (!onlineUsersPollTimer) return;
    clearInterval(onlineUsersPollTimer);
    onlineUsersPollTimer = null;
}

function updateCallHelp(users = callState.lastSeenUsers || []) {
    if (!callState.helpEl) return;
    callState.lastSeenUsers = users;
    let message = '';
    let variant = 'info';

    if (requiresSecureMediaContext) {
        message = 'Browsers block camera/mic on HTTP when using a LAN URL. Access the console over HTTPS (for example via Cloudflare Tunnel) or from localhost to place calls.';
        variant = 'warn';
    } else if (!callState.username) {
        message = 'Choose a display name and click "Go Online" to enable the call controls.';
    } else {
        const others = users.filter(u => u.name !== callState.username);
        if (!others.length) {
            message = 'Waiting for another operator to appear online. Open a second browser window or device to test calls.';
            variant = 'warn';
        }
    }

    if (message) {
        setStatus(callState.helpEl, message, variant);
    } else {
        callState.helpEl.style.display = 'none';
    }
}

function updateBroadcastViewerCount() {
    if (!broadcastState.viewerCountEl) return;
    const count = broadcastState.cameraId ? (viewerCounts[broadcastState.cameraId] || 0) : 0;
    broadcastState.viewerCountEl.textContent = count;
    broadcastState.lastViewerCount = count;
}

function updateCamerasList(cameras = []) {
    knownCameras = cameras.slice();
    const list = document.getElementById('camerasList');
    if (!cameras.length) {
        list.innerHTML = '<div class="no-cameras">No cameras broadcasting. Start one from the Broadcast tab.</div>';
        return;
    }
    list.innerHTML = cameras.map(cid => {
        const count = viewerCounts[cid] || 0;
        const label = count === 1 ? 'watching' : 'watching';
        return `
            <a href="#" class="camera-card" data-camera="${cid}">
                <div class="name">${cid}</div>
                <div class="status">
                    <span class="badge">Live</span>
                    <span class="count-chip">${count} ${label}</span>
                </div>
            </a>
        `;
    }).join('');
    list.querySelectorAll('.camera-card').forEach(card => {
        card.addEventListener('click', (e) => {
            e.preventDefault();
            const cameraId = card.getAttribute('data-camera');
            watchCamera(cameraId);
        });
    });
}

function handleViewerCountUpdate(cameraId, count) {
    viewerCounts[cameraId] = count;
    updateBroadcastViewerCount();
    updateCamerasList(knownCameras);
    if (broadcastState.active && cameraId === broadcastState.cameraId && broadcastState.mode === 'viewer-triggered') {
        if (count > 0) {
            ensureBroadcastStream('viewer connected').catch(err => console.error('Stream start failed', err));
        } else {
            releaseBroadcastStream('No viewers');
        }
    }
}

async function startBroadcast() {
    if (broadcastState.active) return;
    const cameraName = document.getElementById('cameraName').value.trim() || `cam-${Date.now()}`;
    const [width, height] = document.getElementById('resolution').value.split('x').map(Number);
    const audio = document.getElementById('audioEnable').value === 'true';
    broadcastState.settings = { width, height, audio };
    broadcastState.mode = broadcastModeSelect.value;
    updateModeHelpText();
    toggleMotionPanel();
    try {
        socket.emit('register_broadcaster', { camera_id: cameraName, name: cameraName });
        broadcastState.active = true;
        broadcastState.cameraId = cameraName;
        broadcastState.buttonEl.textContent = 'Stop Broadcasting';
        broadcastState.buttonEl.onclick = stopBroadcast;
        broadcastState.mediaControlsEl.style.display = 'none';
        setStatus(broadcastState.statusEl, `Preparing "${cameraName}"`, 'info');
        updateBroadcastViewerCount();
        broadcastState.heartbeatTimer = setInterval(() => {
            socket.emit('broadcaster_heartbeat', { camera_id: cameraName });
        }, 5000);
        broadcastState.viewerPollTimer = setInterval(() => {
            socket.emit('request_viewer_counts');
        }, 10000);
        socket.emit('request_viewer_counts');
        await wakeLockManager.enable();
        if (broadcastState.mode === 'always-on') {
            await ensureBroadcastStream('continuous monitoring');
        } else if (broadcastState.lastViewerCount > 0) {
            await ensureBroadcastStream('viewer connected');
        } else {
            releaseBroadcastStream();
            setStatus(broadcastState.statusEl, 'Standby until a viewer joins.', 'info');
        }
    } catch (err) {
        socket.emit('stop_broadcast', { camera_id: cameraName });
        stopBroadcast({ skipEmit: true });
        setStatus(broadcastState.statusEl, err?.message || 'Failed to start broadcast', 'error');
    }
}

function stopBroadcast(options = {}) {
    const { skipEmit = false } = options;
    if (!broadcastState.active && !broadcastState.stream) return;
    releaseBroadcastStream();
    if (broadcastState.heartbeatTimer) {
        clearInterval(broadcastState.heartbeatTimer);
        broadcastState.heartbeatTimer = null;
    }
    if (broadcastState.viewerPollTimer) {
        clearInterval(broadcastState.viewerPollTimer);
        broadcastState.viewerPollTimer = null;
    }
    if (!skipEmit && broadcastState.cameraId) {
        socket.emit('stop_broadcast', { camera_id: broadcastState.cameraId });
    }
    Object.values(broadcastPeerConnections).forEach(pc => pc.close());
    Object.keys(broadcastPeerConnections).forEach(key => delete broadcastPeerConnections[key]);
    broadcastState.active = false;
    broadcastState.cameraId = null;
    broadcastState.buttonEl.textContent = 'Start Broadcasting';
    broadcastState.buttonEl.onclick = startBroadcast;
    broadcastState.statusEl.style.display = 'none';
    broadcastState.mediaControlsEl.style.display = 'none';
    broadcastState.settings = null;
    broadcastState.lastViewerCount = 0;
    broadcastState.pendingStreamPromise = null;
    wakeLockManager.disable();
    localVideoEl.srcObject = null;
    updateBroadcastViewerCount();
}

function toggleBroadcastMic() {
    if (!broadcastState.stream) return;
    broadcastState.audioEnabled = !broadcastState.audioEnabled;
    broadcastState.stream.getAudioTracks().forEach(track => {
        track.enabled = broadcastState.audioEnabled;
    });
    updateBroadcastMediaButtons();
}

function toggleBroadcastVideo() {
    if (!broadcastState.stream) return;
    broadcastState.videoEnabled = !broadcastState.videoEnabled;
    broadcastState.stream.getVideoTracks().forEach(track => {
        track.enabled = broadcastState.videoEnabled;
    });
    updateBroadcastMediaButtons();
}

function updateBroadcastMediaButtons() {
    if (broadcastState.micBtn) {
        broadcastState.micBtn.classList.toggle('active', broadcastState.audioEnabled);
        broadcastState.micBtn.classList.toggle('muted', !broadcastState.audioEnabled);
        broadcastState.micBtn.innerHTML = `<span>${broadcastState.audioEnabled ? '🎤' : '🔇'}</span> Mic`;
    }
    if (broadcastState.videoBtn) {
        broadcastState.videoBtn.classList.toggle('active', broadcastState.videoEnabled);
        broadcastState.videoBtn.classList.toggle('muted', !broadcastState.videoEnabled);
        broadcastState.videoBtn.innerHTML = `<span>${broadcastState.videoEnabled ? '📹' : '📷'}</span> Video`;
    }
}

broadcastState.micBtn.onclick = toggleBroadcastMic;
broadcastState.videoBtn.onclick = toggleBroadcastVideo;

broadcastModeSelect.addEventListener('change', () => {
    broadcastState.mode = broadcastModeSelect.value;
    updateModeHelpText();
    toggleMotionPanel();
    if (broadcastState.active) {
        if (broadcastState.mode === 'viewer-triggered' && broadcastState.stream && !broadcastState.lastViewerCount) {
            releaseBroadcastStream('Standby mode');
        }
        if (broadcastState.mode === 'always-on' && broadcastState.stream) {
            startMotionDetection();
        }
    }
});

updateModeHelpText();
toggleMotionPanel();

function createBroadcastPeerConnection(peerId, isBroadcaster) {
    const pc = new RTCPeerConnection({
        iceServers: TURN_SERVERS,
        iceTransportPolicy: 'all',
        iceCandidatePoolSize: 10
    });
    pc.onicecandidate = (event) => {
        if (event.candidate) {
            socket.emit('ice_candidate', { target_sid: peerId, candidate: event.candidate });
        }
    };
    pc.onconnectionstatechange = () => {
        if (!isBroadcaster) {
            const state = pc.connectionState;
            if (state === 'connected') {
                setStatus(document.getElementById('watchStatus'), 'Connected', 'success');
            } else if (state === 'failed' || state === 'disconnected') {
                setStatus(document.getElementById('watchStatus'), 'Reconnecting...', 'warn');
                scheduleWatchRetry();
            }
        }
    };
    if (isBroadcaster) {
        pc.ontrack = () => {};
    }
    broadcastPeerConnections[peerId] = pc;
    return pc;
}

async function watchCamera(cameraId, isRetry = false) {
    try {
        watchState.manualClose = false;
        if (isRetry) {
            closeRemoteVideo({ keepModal: true, skipEmit: true, manual: false });
        } else {
            closeRemoteVideo({ keepModal: true, skipEmit: false, manual: false });
        }
        watchState.cameraId = cameraId;
        socket.emit('viewer_join', { camera_id: cameraId });
        document.getElementById('videoPlayerModal').style.display = 'block';
        setStatus(document.getElementById('watchStatus'), `Connecting to ${cameraId}...`, 'info');
        const pc = createBroadcastPeerConnection(cameraId, false);
        watchState.peer = pc;
        pc.ontrack = (event) => {
            watchState.remoteStream = event.streams[0];
            const video = document.getElementById('remoteVideo');
            video.srcObject = watchState.remoteStream;
            video.play().catch(() => video.play().catch(() => {}));
        };
        pc.addTransceiver('video', { direction: 'recvonly' });
        pc.addTransceiver('audio', { direction: 'recvonly' });
        const offer = await pc.createOffer();
        offer.sdp = preferCodec(offer.sdp, 'video', 'VP8');
        offer.sdp = preferCodec(offer.sdp, 'audio', 'opus');
        await pc.setLocalDescription(offer);
        socket.emit('viewer_offer', { camera_id: cameraId, sdp: offer.sdp });
    } catch (err) {
        console.error(err);
        setStatus(document.getElementById('watchStatus'), err.message || 'Unable to view camera', 'error');
    }
}

function scheduleWatchRetry() {
    if (watchState.manualClose || watchState.retryTimer || !watchState.cameraId) return;
    watchState.retryTimer = setTimeout(() => {
        watchState.retryTimer = null;
        if (watchState.cameraId) {
            watchCamera(watchState.cameraId, true);
        }
    }, 3000);
}

function closeRemoteVideo({ keepModal = false, skipEmit = false, manual = true } = {}) {
    if (watchState.retryTimer) {
        clearTimeout(watchState.retryTimer);
        watchState.retryTimer = null;
    }
    const previousCamera = watchState.cameraId;
    if (!keepModal) {
        document.getElementById('videoPlayerModal').style.display = 'none';
    }
    if (watchState.peer) {
        watchState.peer.close();
        watchState.peer = null;
    }
    if (watchState.remoteStream) {
        watchState.remoteStream.getTracks().forEach(track => track.stop());
        watchState.remoteStream = null;
    }
    if (previousCamera && !skipEmit) {
        socket.emit('viewer_leave', { camera_id: previousCamera });
    }
    watchState.manualClose = manual;
    watchState.cameraId = keepModal ? previousCamera : null;
}

function preferCodec(sdp, kind, codec) {
    const lines = sdp.split('\r\n');
    const mLineIndex = lines.findIndex(line => line.startsWith(`m=${kind}`));
    if (mLineIndex === -1) return sdp;
    const codecRegex = new RegExp(`rtpmap:(\\d+) ${codec}`, 'i');
    const payloadTypes = [];
    for (let i = mLineIndex + 1; i < lines.length; i++) {
        if (lines[i].startsWith('m=')) break;
        const match = lines[i].match(codecRegex);
        if (match) payloadTypes.push(match[1]);
    }
    if (!payloadTypes.length) return sdp;
    const mLineParts = lines[mLineIndex].split(' ');
    const others = mLineParts.slice(3).filter(pt => !payloadTypes.includes(pt));
    lines[mLineIndex] = [...mLineParts.slice(0, 3), ...payloadTypes, ...others].join(' ');
    return lines.join('\r\n');
}

function toggleFullscreen(videoElement) {
    if (!videoElement) return;
    const wrapper = videoElement.closest('.video-wrapper') || videoElement.parentElement;
    if (!document.fullscreenElement) {
        if (wrapper.requestFullscreen) {
            wrapper.requestFullscreen();
        } else if (wrapper.webkitRequestFullscreen) {
            wrapper.webkitRequestFullscreen();
        } else if (wrapper.msRequestFullscreen) {
            wrapper.msRequestFullscreen();
        }
    } else {
        if (document.exitFullscreen) {
            document.exitFullscreen();
        } else if (document.webkitExitFullscreen) {
            document.webkitExitFullscreen();
        } else if (document.msExitFullscreen) {
            document.msExitFullscreen();
        }
    }
}

function togglePictureInPicture(videoElement) {
    if (!videoElement || !document.pictureInPictureEnabled) return;
    if (document.pictureInPictureElement === videoElement) {
        document.exitPictureInPicture();
    } else {
        videoElement.requestPictureInPicture().catch(() => {
            setStatus(document.getElementById('watchStatus'), 'PiP not supported', 'warn');
        });
    }
}

document.getElementById('watchFullscreenBtn').onclick = () => {
    toggleFullscreen(document.getElementById('remoteVideo'));
};
document.getElementById('watchPipBtn').onclick = () => {
    togglePictureInPicture(document.getElementById('remoteVideo'));
};
document.getElementById('callLocalFullscreenBtn').onclick = () => {
    toggleFullscreen(document.getElementById('callLocalVideo'));
};

// -------------------- Call Hub --------------------

function renderOnlineUsers(users = []) {
    callState.lastSeenUsers = users.slice();
    if (!users.length) {
        callState.onlineUsersEl.innerHTML = '<div class="call-empty-state">No one is online yet.</div>';
        updateCallHelp(users);
        return;
    }
    callState.onlineUsersEl.innerHTML = users.map(user => {
        const busy = user.in_call;
        const meta = busy ? 'In a call' : (user.auto_pickup ? 'Auto pickup on' : 'Manual pickup');
        let disableReason = '';
        if (!callState.username) {
            disableReason = 'Click "Go Online" to start calling.';
        } else if (user.name === callState.username) {
            disableReason = 'This is you.';
        } else if (busy) {
            disableReason = `${user.name} is already in a call.`;
        }
        const disabled = Boolean(disableReason);
        const titleAttr = disableReason ? `title="${disableReason.replace(/"/g, '&quot;')}"` : '';
        return `
            <div class="online-user-card ${busy ? 'busy' : ''}" data-user="${user.name}">
                <div>
                    <div style="font-weight:600;">${user.name}</div>
                    <div class="meta">${meta}</div>
                </div>
                <button class="btn btn-secondary" ${disabled ? 'disabled' : ''} ${titleAttr}>Call</button>
            </div>
        `;
    }).join('');
    callState.onlineUsersEl.querySelectorAll('.online-user-card').forEach(card => {
        const target = card.getAttribute('data-user');
        const button = card.querySelector('button');
        if (button && !button.disabled) {
            button.addEventListener('click', () => startCall(target));
        }
    });
    updateCallHelp(users);
}

function updatePresenceChip() {
    if (!callState.presenceChip) return;
    if (!callState.username) {
        callState.presenceChip.textContent = 'Offline';
        callState.presenceChip.className = 'call-status-chip idle';
        return;
    }
    callState.presenceChip.textContent = callState.callId ? 'In Call' : 'Online';
    callState.presenceChip.className = 'call-status-chip ' + (callState.callId ? 'live' : 'idle');
}

async function ensureCallLocalStream() {
    if (callState.localStream) return callState.localStream;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        setStatus(callState.statusEl, 'Camera/microphone capture is not supported in this browser.', 'error');
        throw new Error('getUserMedia unsupported');
    }
    try {
        callState.localStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
        callState.localVideo.srcObject = callState.localStream;
        callState.audioEnabled = true;
        callState.videoEnabled = true;
        updateCallMediaButtons();
        return callState.localStream;
    } catch (err) {
        let message = err?.message || 'Unable to access camera/microphone.';
        if (requiresSecureMediaContext) {
            message = 'Browser blocked camera/mic because this page was loaded over HTTP. Use HTTPS or run from localhost to place calls.';
        } else if (err?.name === 'NotAllowedError') {
            message = 'Permission to use camera/microphone was denied. Allow access and try again.';
        }
        setStatus(callState.statusEl, message, 'error');
        throw err;
    }
}

function toggleCallMic() {
    if (!callState.localStream) return;
    callState.audioEnabled = !callState.audioEnabled;
    callState.localStream.getAudioTracks().forEach(track => {
        track.enabled = callState.audioEnabled;
    });
    updateCallMediaButtons();
}

function toggleCallVideo() {
    if (!callState.localStream) return;
    callState.videoEnabled = !callState.videoEnabled;
    callState.localStream.getVideoTracks().forEach(track => {
        track.enabled = callState.videoEnabled;
    });
    updateCallMediaButtons();
}

function updateCallMediaButtons() {
    if (callState.micBtn) {
        callState.micBtn.classList.toggle('active', callState.audioEnabled);
        callState.micBtn.classList.toggle('muted', !callState.audioEnabled);
        callState.micBtn.innerHTML = `<span>${callState.audioEnabled ? '🎤' : '🔇'}</span> Mic`;
    }
    if (callState.videoBtn) {
        callState.videoBtn.classList.toggle('active', callState.videoEnabled);
        callState.videoBtn.classList.toggle('muted', !callState.videoEnabled);
        callState.videoBtn.innerHTML = `<span>${callState.videoEnabled ? '📹' : '📷'}</span> Video`;
    }
    if (callState.muteIndicator) {
        callState.muteIndicator.classList.toggle('show', !callState.audioEnabled);
    }
}

function teardownCall(message = 'Call ended') {
    if (callState.pendingCallId) {
        callState.pendingCallId = null;
    }
    Object.values(callState.peers).forEach(pc => pc.close());
    callState.peers = {};
    callState.remoteStreams = {};
    callState.remoteGrid.innerHTML = '<div class="call-empty-state">Remote video feeds will appear here when you join a call.</div>';
    if (callState.localStream) {
        callState.localStream.getTracks().forEach(track => track.stop());
        callState.localStream = null;
        callState.localVideo.srcObject = null;
    }
    callState.callId = null;
    callState.participants = [];
    callState.leaveBtn.disabled = true;
    callState.incomingBanner.classList.add('hidden');
    callState.incomingCallId = null;
    callState.micBtn.disabled = true;
    callState.videoBtn.disabled = true;
    renderCallParticipants();
    setStatus(callState.statusEl, message, 'info');
    updatePresenceChip();
}

function renderCallParticipants() {
    if (!callState.participants.length) {
        callState.participantsEl.innerHTML = '<span class="pill">No active call</span>';
        return;
    }
    callState.participantsEl.innerHTML = callState.participants.map(name => `<span class="pill">${name}</span>`).join('');
}

function ensureRemoteVideo(user) {
    let tile = document.getElementById(`remote-${user}`);
    if (!tile) {
        tile = document.createElement('div');
        tile.className = 'video-tile';
        tile.id = `remote-${user}`;
        tile.innerHTML = `<div class="video-label">${user}</div><video autoplay playsinline class="call-video"></video>`;
        if (callState.remoteGrid.children.length === 1 && callState.remoteGrid.firstElementChild.classList.contains('call-empty-state')) {
            callState.remoteGrid.innerHTML = '';
        }
        callState.remoteGrid.appendChild(tile);
    }
    return tile.querySelector('video');
}

function removeRemoteVideo(user) {
    const tile = document.getElementById(`remote-${user}`);
    if (tile) {
        const video = tile.querySelector('video');
        if (video && video.srcObject) {
            video.srcObject.getTracks().forEach(track => track.stop());
        }
        tile.remove();
    }
    if (!callState.remoteGrid.children.length) {
        callState.remoteGrid.innerHTML = '<div class="call-empty-state">Remote video feeds will appear here when you join a call.</div>';
    }
}

function createCallPeer(target) {
    const pc = new RTCPeerConnection({ iceServers: TURN_SERVERS, iceTransportPolicy: 'all' });
    if (callState.localStream) {
        callState.localStream.getTracks().forEach(track => pc.addTrack(track, callState.localStream));
    }
    pc.onicecandidate = (event) => {
        if (event.candidate) {
            socket.emit('call_webrtc_ice', { call_id: callState.callId, target, candidate: event.candidate });
        }
    };
    pc.ontrack = (event) => {
        const video = ensureRemoteVideo(target);
        video.srcObject = event.streams[0];
    };
    pc.onconnectionstatechange = () => {
        if (pc.connectionState === 'failed') {
            pc.restartIce?.();
        }
    };
    callState.peers[target] = pc;
    return pc;
}

async function connectToParticipant(user) {
    if (!callState.callId || user === callState.username) return;
    await ensureCallLocalStream();
    let pc = callState.peers[user];
    if (!pc) pc = createCallPeer(user);
    const shouldInitiate = callState.username.localeCompare(user) < 0;
    if (shouldInitiate && !pc.__initiated) {
        pc.__initiated = true;
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        socket.emit('call_webrtc_offer', { call_id: callState.callId, target: user, sdp: offer.sdp });
    }
}

function syncCallPeers() {
    callState.participants.forEach(user => {
        if (user === callState.username) return;
        connectToParticipant(user);
    });
    Object.keys(callState.peers).forEach(user => {
        if (!callState.participants.includes(user)) {
            callState.peers[user].close();
            delete callState.peers[user];
            removeRemoteVideo(user);
        }
    });
    renderCallParticipants();
    updatePresenceChip();
}

function startCall(target) {
    if (!callState.username) {
        setStatus(callState.statusEl, 'Save a profile first.', 'warn');
        return;
    }
    socket.emit('call_user', { target });
    setStatus(callState.statusEl, `Calling ${target}...`, 'info');
}

function saveCallProfile() {
    const name = callNameInput.value.trim();
    if (!name) {
        setStatus(callState.statusEl, 'Enter a display name to go online.', 'warn');
        return;
    }
    const autoPickup = autoPickupToggle.checked;
    socket.emit('register_user', { name, auto_pickup: autoPickup });
    localStorage.setItem('callName', name);
    localStorage.setItem('callAutoPickup', String(autoPickup));
}

function leaveCall() {
    if (!callState.callId) return;
    socket.emit('leave_call');
    teardownCall('Left call');
}

saveCallProfileBtn.addEventListener('click', saveCallProfile);
callNameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveCallProfile(); });
autoPickupToggle.addEventListener('change', () => {
    if (callState.username) {
        socket.emit('update_auto_pickup', { enabled: autoPickupToggle.checked });
    }
});
callState.leaveBtn.addEventListener('click', leaveCall);
callState.micBtn.addEventListener('click', toggleCallMic);
callState.videoBtn.addEventListener('click', toggleCallVideo);
closeRemoteBtn.addEventListener('click', () => closeRemoteVideo({ manual: true }));

// Restore saved profile
const storedName = localStorage.getItem('callName');
const storedAuto = localStorage.getItem('callAutoPickup');
if (storedName) callNameInput.value = storedName;
if (storedAuto !== null) autoPickupToggle.checked = storedAuto === 'true';
updateCallHelp();

// -------------------- Socket Events --------------------

socket.on('connect', () => {
    socket.emit('request_viewer_counts');
    socket.emit('request_online_users');
    startOnlineUsersPolling();
});

socket.on('disconnect', () => {
    stopOnlineUsersPolling();
});

socket.on('broadcasters_list', (data) => {
    updateCamerasList(data?.broadcasters || []);
});

socket.on('broadcaster_left', (data) => {
    if (watchState.cameraId === data?.camera_id) {
        setStatus(document.getElementById('watchStatus'), 'Broadcaster went offline.', 'warn');
        scheduleWatchRetry();
    }
});

socket.on('viewer_count', (data) => {
    handleViewerCountUpdate(data.camera_id, data.count);
});

socket.on('viewer_counts', (data) => {
    Object.keys(viewerCounts).forEach(key => delete viewerCounts[key]);
    Object.entries(data.counts || {}).forEach(([cid, count]) => viewerCounts[cid] = count);
    updateBroadcastViewerCount();
    updateCamerasList(knownCameras);
});

socket.on('viewer_offer', async (data) => {
    if (!broadcastState.active) return;
    try {
        await ensureBroadcastStream('viewer connected');
    } catch (err) {
        console.error('Unable to start stream for viewer', err);
        return;
    }
    if (!broadcastState.stream) return;
    const pc = createBroadcastPeerConnection(data.viewer_sid, true);
    broadcastState.stream.getTracks().forEach(track => pc.addTrack(track, broadcastState.stream));
    await pc.setRemoteDescription(new RTCSessionDescription({ type: 'offer', sdp: data.sdp }));
    let answer = await pc.createAnswer();
    answer.sdp = preferCodec(answer.sdp, 'video', 'VP8');
    answer.sdp = preferCodec(answer.sdp, 'audio', 'opus');
    await pc.setLocalDescription(answer);
    socket.emit('broadcaster_answer', { viewer_sid: data.viewer_sid, sdp: answer.sdp });
});

socket.on('broadcaster_answer', async (data) => {
    if (watchState.peer && watchState.peer.signalingState === 'have-local-offer') {
        await watchState.peer.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: data.sdp }));
        setStatus(document.getElementById('watchStatus'), 'Connected', 'success');
    }
});

socket.on('ice_candidate', (data) => {
    const pcViewer = broadcastPeerConnections[data.from_sid];
    if (pcViewer) {
        pcViewer.addIceCandidate(new RTCIceCandidate(data.candidate)).catch(console.warn);
    }
    if (watchState.peer) {
        watchState.peer.addIceCandidate(new RTCIceCandidate(data.candidate)).catch(console.warn);
    }
});

socket.on('user_registered', (data) => {
    callState.username = data.name;
    callState.autoPickup = data.auto_pickup;
    autoPickupToggle.checked = data.auto_pickup;
    setStatus(callState.statusEl, `Online as ${data.name}`, 'success');
    updatePresenceChip();
    updateCallHelp(callState.lastSeenUsers || []);
    socket.emit('request_online_users');
});

socket.on('online_users', (data) => {
    renderOnlineUsers(data.users || []);
});

socket.on('call_pending', (data) => {
    callState.pendingCallId = data.call_id;
    setStatus(callState.statusEl, `Waiting for ${data.target}...`, 'info');
});

socket.on('incoming_call', (data) => {
    callState.incomingBanner.classList.remove('hidden');
    callState.incomingCallId = data.call_id;
    callState.incomingText.textContent = `${data.from} is calling...`;
    callState.acceptBtn.onclick = () => {
        socket.emit('respond_call', { call_id: data.call_id, accept: true });
        callState.incomingBanner.classList.add('hidden');
    };
    callState.declineBtn.onclick = () => {
        socket.emit('respond_call', { call_id: data.call_id, accept: false });
        callState.incomingBanner.classList.add('hidden');
    };
});

socket.on('call_declined', () => {
    setStatus(callState.statusEl, 'Call declined.', 'warn');
    callState.pendingCallId = null;
});

socket.on('call_joined', async (payload) => {
    callState.callId = payload.call_id;
    callState.participants = payload.participants || [];
    callState.leaveBtn.disabled = false;
    callState.micBtn.disabled = false;
    callState.videoBtn.disabled = false;
    setStatus(callState.statusEl, 'In call', 'success');
    callState.incomingBanner.classList.add('hidden');
    await ensureCallLocalStream();
    syncCallPeers();
});

socket.on('call_participant_joined', (data) => {
    callState.participants = data.participants || callState.participants;
    syncCallPeers();
    setStatus(callState.statusEl, `${data.user} joined the call`, 'info');
});

socket.on('call_participant_left', (data) => {
    callState.participants = data.participants || callState.participants.filter(p => p !== data.user);
    const pc = callState.peers[data.user];
    if (pc) {
        pc.close();
        delete callState.peers[data.user];
    }
    removeRemoteVideo(data.user);
    syncCallPeers();
    setStatus(callState.statusEl, `${data.user} left the call`, 'warn');
});

socket.on('call_ended', () => {
    teardownCall('Call ended');
});

socket.on('call_error', (data) => {
    setStatus(callState.statusEl, data.message || 'Call error', 'error');
});

socket.on('call_webrtc_offer', async (data) => {
    if (data.call_id !== callState.callId) return;
    await ensureCallLocalStream();
    let pc = callState.peers[data.from];
    if (!pc) pc = createCallPeer(data.from);
    await pc.setRemoteDescription(new RTCSessionDescription({ type: 'offer', sdp: data.sdp }));
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    socket.emit('call_webrtc_answer', { call_id: callState.callId, target: data.from, sdp: answer.sdp });
});

socket.on('call_webrtc_answer', async (data) => {
    const pc = callState.peers[data.from];
    if (pc && pc.signalingState === 'have-local-offer') {
        await pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: data.sdp }));
    }
});

socket.on('call_webrtc_ice', (data) => {
    const pc = callState.peers[data.from];
    if (pc) {
        pc.addIceCandidate(new RTCIceCandidate(data.candidate)).catch(console.warn);
    }
});

window.addEventListener('beforeunload', () => {
    if (broadcastState.active) stopBroadcast();
    closeRemoteVideo({ manual: true });
    if (callState.callId) socket.emit('leave_call');
});