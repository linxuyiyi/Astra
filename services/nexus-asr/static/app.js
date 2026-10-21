'use strict';

const API = '/api/asr';
const MAX_RECORD_MS = 60_000;
const MAX_UPLOAD_BYTES = 12 * 1024 * 1024;

const els = {
  healthPill: document.getElementById('healthPill'),
  healthText: document.getElementById('healthText'),
  micBtn: document.getElementById('micBtn'),
  timer: document.getElementById('timer'),
  recordHint: document.getElementById('recordHint'),
  transcript: document.getElementById('transcript'),
  retryBtn: document.getElementById('retryBtn'),
  clearBtn: document.getElementById('clearBtn'),
  copyBtn: document.getElementById('copyBtn'),
  uploadBtn: document.getElementById('uploadBtn'),
  fileInput: document.getElementById('fileInput'),
  statDuration: document.getElementById('statDuration'),
  statTotal: document.getElementById('statTotal'),
  statElapsed: document.getElementById('statElapsed'),
  statDecode: document.getElementById('statDecode'),
  statRtf: document.getElementById('statRtf'),
  statBytes: document.getElementById('statBytes'),
  mModel: document.getElementById('mModel'),
  mLoad: document.getElementById('mLoad'),
  mThreads: document.getElementById('mThreads'),
  mUptime: document.getElementById('mUptime'),
  mTotal: document.getElementById('mTotal'),
  mActive: document.getElementById('mActive'),
  modelTag: document.getElementById('modelTag'),
  reqTag: document.getElementById('reqTag'),
  rLang: document.getElementById('rLang'),
  rFormat: document.getElementById('rFormat'),
  rId: document.getElementById('rId'),
  rClient: document.getElementById('rClient'),
  rChars: document.getElementById('rChars'),
  rStatus: document.getElementById('rStatus'),
  rawJson: document.getElementById('rawJson'),
  errorBox: document.getElementById('errorBox'),
  toast: document.getElementById('toast'),
};

const state = {
  phase: 'idle',
  stream: null,
  recorder: null,
  chunks: [],
  startedAt: 0,
  timerId: null,
  autoStopId: null,
  lastBlob: null,
  lastFilename: '',
  serverReady: false,
};

let toastTimer = null;

function setPhase(phase) {
  state.phase = phase;
  const recording = phase === 'recording';
  const busy = phase === 'requesting' || phase === 'transcribing';
  els.micBtn.classList.toggle('recording', recording);
  els.micBtn.classList.toggle('busy', busy);
  els.micBtn.disabled = !state.serverReady || busy;
  els.micBtn.setAttribute('aria-label', recording ? '结束录音' : '开始录音');
  els.uploadBtn.disabled = busy || recording;
  els.retryBtn.disabled = busy || recording || !state.lastBlob;

  const hints = {
    idle: '点击麦克风开始',
    requesting: '正在请求麦克风…',
    recording: '录音中，再次点击结束',
    transcribing: '上传并识别中…',
    error: '出错了，可重试',
  };
  els.recordHint.textContent = hints[phase] || '';
  if (phase !== 'recording' && phase !== 'transcribing') {
    els.micBtn.disabled = !state.serverReady;
  }
}

function formatMs(ms) {
  if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

function formatBytes(bytes) {
  if (!bytes) return '—';
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function showError(message) {
  els.errorBox.hidden = false;
  els.errorBox.textContent = message;
}

function clearError() {
  els.errorBox.hidden = true;
  els.errorBox.textContent = '';
}

function toast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.classList.toggle('error', isError);
  els.toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.remove('show'), 3200);
}

function setHealth(ok, text) {
  state.serverReady = ok;
  els.healthPill.dataset.state = ok ? 'ok' : 'error';
  els.healthText.textContent = text;
  if (!ok) {
    els.micBtn.disabled = true;
    els.modelTag.textContent = '未就绪';
  } else if (state.phase === 'idle' || state.phase === 'error') {
    els.micBtn.disabled = false;
  }
}

function updateTimer() {
  const elapsed = performance.now() - state.startedAt;
  const seconds = Math.floor(elapsed / 1000);
  const mm = String(Math.floor(seconds / 60)).padStart(2, '0');
  const ss = String(seconds % 60).padStart(2, '0');
  els.timer.textContent = `${mm}:${ss}`;
  if (elapsed > MAX_RECORD_MS - 5000 && state.phase === 'recording') {
    els.recordHint.textContent = '即将到达 60 秒上限';
  }
}

async function refreshHealth() {
  try {
    const health = await fetch('/health', { cache: 'no-store' });
    const body = await health.json().catch(() => ({}));
    if (!health.ok) {
      setHealth(false, body.status === 'loading' ? '模型加载中…' : '服务不可用');
      return;
    }
    setHealth(true, '服务就绪');
    els.mModel.textContent = body.model || '—';
    els.mTotal.textContent = body.total_requests ?? '—';
    els.mActive.textContent = body.active_requests ?? '—';
    els.mUptime.textContent = `${Math.round(body.uptime_seconds || 0)} s`;

    const models = await fetch(`${API}/models`, { cache: 'no-store' }).then((r) => r.json());
    const model = models.models && models.models[0];
    if (model) {
      els.modelTag.textContent = model.loaded ? 'loaded' : 'loading';
      els.mLoad.textContent = formatMs(model.load_ms);
      els.mThreads.textContent = `${model.num_threads} 线程`;
      els.mModel.textContent = model.id;
    }
  } catch (err) {
    setHealth(false, '无法连接服务');
    console.warn(err);
  }
}

function supportedMimeType() {
  if (!window.MediaRecorder) return '';
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/ogg;codecs=opus',
  ];
  return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || '';
}

async function startRecording() {
  if (state.phase === 'recording') {
    stopRecording('user');
    return;
  }
  if (state.phase !== 'idle' && state.phase !== 'error') return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showError('浏览器不支持 getUserMedia，请使用 Chrome 或 Edge。');
    return;
  }
  if (!window.MediaRecorder) {
    showError('浏览器不支持 MediaRecorder，无法录音。');
    return;
  }

  clearError();
  setPhase('requesting');
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    const mimeType = supportedMimeType();
    state.recorder = mimeType
      ? new MediaRecorder(state.stream, { mimeType })
      : new MediaRecorder(state.stream);
    state.chunks = [];
    state.recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) state.chunks.push(event.data);
    };
    state.recorder.onstop = () => {
      const blob = new Blob(state.chunks, { type: state.recorder?.mimeType || 'audio/webm' });
      const mime = state.recorder?.mimeType || blob.type || 'audio/webm';
      const ext = mime.includes('mp4') ? 'm4a' : mime.includes('ogg') ? 'ogg' : 'webm';
      cleanupMedia();
      if (blob.size < 500) {
        setPhase('error');
        showError('录音数据为空，请检查麦克风后重试。');
        return;
      }
      transcribeBlob(blob, `recording.${ext}`);
    };
    state.recorder.start(200);
    state.startedAt = performance.now();
    els.timer.textContent = '00:00';
    setPhase('recording');
    state.timerId = setInterval(updateTimer, 100);
    state.autoStopId = setTimeout(() => {
      if (state.phase === 'recording') {
        toast('已达到 60 秒上限，自动结束录音');
        stopRecording('limit');
      }
    }, MAX_RECORD_MS);
  } catch (err) {
    cleanupMedia();
    setPhase('error');
    showError(micErrorMessage(err));
  }
}

function stopRecording(reason = 'user') {
  if (state.phase !== 'recording') return;
  clearInterval(state.timerId);
  clearTimeout(state.autoStopId);
  state.timerId = null;
  state.autoStopId = null;
  setPhase('transcribing');
  els.rStatus.textContent = '识别中';
  try {
    if (state.recorder && state.recorder.state !== 'inactive') {
      state.recorder.stop();
    }
  } catch (err) {
    cleanupMedia();
    setPhase('error');
    showError(`停止录音失败：${err.message || err}`);
  }
}

function cleanupMedia() {
  clearInterval(state.timerId);
  clearTimeout(state.autoStopId);
  state.timerId = null;
  state.autoStopId = null;
  if (state.stream) {
    state.stream.getTracks().forEach((track) => track.stop());
    state.stream = null;
  }
  state.recorder = null;
}

function micErrorMessage(err) {
  switch (err && err.name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return '麦克风权限被拒绝。请点击地址栏左侧的锁图标，把麦克风设为允许后重试。';
    case 'NotFoundError':
      return '没有找到麦克风设备。';
    case 'NotReadableError':
      return '麦克风被其他应用占用。';
    case 'OverconstrainedError':
      return '麦克风不支持请求的参数。';
    default:
      return err && err.message ? err.message : '无法打开麦克风。';
  }
}

async function transcribeBlob(blob, filename) {
  if (!blob || blob.size === 0) {
    setPhase('error');
    showError('没有可识别的音频。');
    return;
  }
  if (blob.size > MAX_UPLOAD_BYTES) {
    setPhase('error');
    showError('录音文件超过 12 MB 上限。');
    return;
  }

  state.lastBlob = blob;
  state.lastFilename = filename;
  setPhase('transcribing');
  els.rStatus.textContent = '上传中';
  els.reqTag.textContent = '请求中';
  clearError();

  const form = new FormData();
  form.append('file', blob, filename);
  const started = performance.now();

  try {
    const response = await fetch(`${API}/transcribe`, { method: 'POST', body: form });
    const body = await response.json().catch(() => ({}));
    const clientMs = performance.now() - started;

    if (!response.ok) {
      const detail = body.detail || {};
      const code = typeof detail === 'object' ? detail.code : '';
      const message = typeof detail === 'object' ? detail.message : String(detail || '');
      throw new Error(errorMessage(code, message, response.status));
    }

    els.transcript.value = body.text || '';
    renderResult(body, blob, clientMs);
    setPhase('idle');
    toast('识别完成');
  } catch (err) {
    setPhase('error');
    els.rStatus.textContent = '失败';
    els.reqTag.textContent = '失败';
    showError(err.message || '识别失败，请重试。');
  }
}

function errorMessage(code, message, status) {
  const map = {
    rate_limited: '请求太频繁，请稍后再试。',
    server_busy: '服务正忙，请稍后重试。',
    audio_too_long: '录音超过 60 秒上限。',
    unsupported_audio: '音频格式无法解码，请重试或上传 WAV/WebM。',
    empty_audio: '没有录到有效声音。',
    recognition_timeout: '识别超时，请重试。',
    payload_too_large: '上传文件超过大小限制。',
    model_not_ready: '模型尚未就绪。',
  };
  return map[code] || message || `请求失败（HTTP ${status}）`;
}

function renderResult(body, blob, clientMs) {
  els.statDuration.textContent = `${Number(body.duration).toFixed(2)} s`;
  els.statTotal.textContent = formatMs(body.total_ms);
  els.statElapsed.textContent = formatMs(body.elapsed_ms);
  els.statDecode.textContent = formatMs(body.decode_ms);
  els.statRtf.textContent = Number(body.rtf).toFixed(4);
  els.statBytes.textContent = formatBytes(blob.size);
  els.rLang.textContent = body.language || 'auto';
  els.rFormat.textContent = (blob.type || 'audio').replace('audio/', '');
  els.rId.textContent = body.request_id || '—';
  els.rClient.textContent = formatMs(clientMs);
  els.rChars.textContent = `${(body.text || '').length} 字`;
  els.rStatus.textContent = '成功';
  els.reqTag.textContent = '完成';
  els.rawJson.textContent = JSON.stringify(body, null, 2);
  els.retryBtn.disabled = false;
}

els.micBtn.addEventListener('click', startRecording);
els.retryBtn.addEventListener('click', () => {
  if (state.lastBlob) transcribeBlob(state.lastBlob, state.lastFilename);
});
els.clearBtn.addEventListener('click', () => {
  els.transcript.value = '';
  clearError();
  els.rStatus.textContent = '—';
  els.reqTag.textContent = '—';
});
els.copyBtn.addEventListener('click', async () => {
  const text = els.transcript.value;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制');
  } catch {
    els.transcript.select();
    document.execCommand('copy');
    toast('已复制');
  }
});
els.uploadBtn.addEventListener('click', () => els.fileInput.click());
els.fileInput.addEventListener('change', () => {
  const file = els.fileInput.files && els.fileInput.files[0];
  els.fileInput.value = '';
  if (!file) return;
  if (file.size > MAX_UPLOAD_BYTES) {
    showError('文件超过 12 MB 上限。');
    return;
  }
  transcribeBlob(file, file.name || 'upload.wav');
});

window.addEventListener('beforeunload', cleanupMedia);

refreshHealth();
setInterval(refreshHealth, 8000);
