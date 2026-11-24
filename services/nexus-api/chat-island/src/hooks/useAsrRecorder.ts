import { useCallback, useEffect, useRef, useState } from 'react';

export type AsrRecorderState = 'idle' | 'recording' | 'transcribing' | 'error';
export type AsrRecorderSource = 'button' | 'hotkey' | null;

interface UseAsrRecorderOptions {
	enabled?: boolean;
	maxSeconds?: number;
	/** The conversation/channel that started the recording. */
	sessionKey?: string;
	onText: (text: string) => void;
}

interface AsrRecorder {
	state: AsrRecorderState;
	seconds: number;
	error: string | null;
	source: AsrRecorderSource;
	requesting: boolean;
	/** Start a new recording. ``hotkey`` keeps the PTT UI distinct. */
	start: (source?: Exclude<AsrRecorderSource, null>) => void;
	/** Stop a recording and transcribe; cancels a pending permission request. */
	stop: () => void;
	/** Discard the current recording/transcription without inserting text. */
	cancel: () => void;
	/** Original microphone-button behaviour. */
	toggle: () => void;
	retry: () => void;
}

const MAX_UPLOAD_BYTES = 12 * 1024 * 1024;
const ASR_ENDPOINT = '/api/asr/transcribe';

const recognitionErrors: Record<string, string> = {
  rate_limited: '请求太频繁，请稍后再试',
  server_busy: '服务繁忙，请稍后重试',
  audio_too_long: '录音超过 60 秒',
  unsupported_audio: '音频格式无法识别',
  empty_audio: '没有识别到语音',
  recognition_timeout: '识别超时，请重试',
  payload_too_large: '录音文件超过大小限制',
  model_not_ready: '语音服务尚未就绪',
};

function mapMicrophoneError(error: unknown): string {
	const name = error instanceof DOMException ? error.name : '';
	if (name === 'NotAllowedError' || name === 'SecurityError') return '麦克风权限被拒绝，请在浏览器设置中允许后重试';
	if (name === 'NotFoundError') return '没有找到麦克风设备';
	if (name === 'NotSupportedError') return '当前浏览器不支持录音';
	if (error instanceof Error && error.message) return error.message;
	return '当前浏览器不支持录音';
}

/**
 * Browser recording -> POST /api/asr/transcribe -> text.
 *
 * Deliberately not a streaming implementation: the server recognises the
 * completed recording. The microphone is released as soon as recording stops;
 * no track is kept alive between rounds.
 *
 * One instance owns at most one MediaRecorder. Button and push-to-talk share
 * this state machine, so they can never record concurrently.
 */
export function useAsrRecorder({
	enabled = true,
	maxSeconds = 60,
	sessionKey = '',
	onText,
}: UseAsrRecorderOptions): AsrRecorder {
	const [state, setState] = useState<AsrRecorderState>('idle');
	const [seconds, setSeconds] = useState(0);
	const [error, setError] = useState<string | null>(null);
	const [source, setSource] = useState<AsrRecorderSource>(null);
	const [requesting, setRequesting] = useState(false);

	const stateRef = useRef<AsrRecorderState>('idle');
	const sourceRef = useRef<AsrRecorderSource>(null);
	const streamRef = useRef<MediaStream | null>(null);
	const recorderRef = useRef<MediaRecorder | null>(null);
	const chunksRef = useRef<Blob[]>([]);
	const timerRef = useRef<number | null>(null);
	const autoStopRef = useRef<number | null>(null);
	const startedAtRef = useRef(0);
	const lastBlobRef = useRef<Blob | null>(null);
	const onTextRef = useRef(onText);
	const mountedRef = useRef(true);
	const startingRef = useRef(false);
	// Every start/cancel gets a token. A getUserMedia promise that resolves
	// after the user released the key must not start a recorder.
	const startTokenRef = useRef(0);
	const transcribeTokenRef = useRef(0);
	const transcribeAbortRef = useRef<AbortController | null>(null);
	const discardRef = useRef(false);
	const sessionKeyRef = useRef(sessionKey);
	const recordingSessionRef = useRef('');

	onTextRef.current = onText;
	sessionKeyRef.current = sessionKey;

	const setPhase = useCallback((next: AsrRecorderState) => {
		stateRef.current = next;
		setState(next);
	}, []);

	const setSourcePhase = useCallback((next: AsrRecorderSource) => {
		sourceRef.current = next;
		setSource(next);
	}, []);

	const clearTimers = useCallback(() => {
		if (timerRef.current !== null) window.clearInterval(timerRef.current);
		if (autoStopRef.current !== null) window.clearTimeout(autoStopRef.current);
		timerRef.current = null;
		autoStopRef.current = null;
	}, []);

	const releaseMic = useCallback(() => {
		if (streamRef.current) {
			streamRef.current.getTracks().forEach((track) => track.stop());
			streamRef.current = null;
		}
	}, []);

	const cleanup = useCallback(() => {
		clearTimers();
		releaseMic();
		recorderRef.current = null;
		chunksRef.current = [];
	}, [clearTimers, releaseMic]);

	const transcribe = useCallback(
		async (blob: Blob, sessionAtStart: string) => {
			const token = ++transcribeTokenRef.current;
			transcribeAbortRef.current?.abort();
			const controller = new AbortController();
			transcribeAbortRef.current = controller;

			setError(null);
			setPhase('transcribing');
			setSourcePhase(null);
			const form = new FormData();
			const ext = blob.type.includes('mp4') ? 'm4a' : blob.type.includes('ogg') ? 'ogg' : 'webm';
			form.append('file', blob, `recording.${ext}`);

			const timeout = window.setTimeout(() => controller.abort(), 60_000);
			try {
				const response = await fetch(ASR_ENDPOINT, {
					method: 'POST',
					body: form,
					signal: controller.signal,
				});
				const body = await response.json().catch(() => ({}));
				if (!response.ok) {
					const detail = body?.detail ?? body?.error;
					const code = typeof detail === 'object' ? detail?.code : '';
					const message = typeof detail === 'string' ? detail : detail?.message;
					throw new Error(
						recognitionErrors[code] || message || `识别失败（HTTP ${response.status}）`,
					);
				}
				const text = String(body?.text || '').trim();
				if (!text) throw new Error('没有识别到语音');
				if (!mountedRef.current || token !== transcribeTokenRef.current) return;
				// A transcript belongs to the conversation that started the
				// recording. Never write it into a conversation opened later.
				if (sessionAtStart !== sessionKeyRef.current) {
					setPhase('idle');
					return;
				}
				onTextRef.current(text);
				setPhase('idle');
			} catch (err) {
				if (!mountedRef.current || token !== transcribeTokenRef.current) return;
				const message =
					err instanceof DOMException && err.name === 'AbortError'
						? '识别超时，请重试'
						: err instanceof Error
							? err.message
							: '识别失败';
				setError(message);
				setPhase('error');
			} finally {
				window.clearTimeout(timeout);
				if (token === transcribeTokenRef.current) transcribeAbortRef.current = null;
			}
		},
		[setPhase, setSourcePhase],
	);

	const start = useCallback(
		(nextSource: Exclude<AsrRecorderSource, null> = 'button') => {
			if (
				!enabled ||
				startingRef.current ||
				stateRef.current === 'recording' ||
				stateRef.current === 'transcribing'
			) {
				return;
			}
			const token = ++startTokenRef.current;
			startingRef.current = true;
			discardRef.current = false;
			recordingSessionRef.current = sessionKeyRef.current;
			setRequesting(true);
			setSourcePhase(nextSource);
			setError(null);
			// A new recording replaces the retry buffer; an old clip must never be
			// re-uploaded after a permission error.
			lastBlobRef.current = null;

			if (!navigator.mediaDevices?.getUserMedia) {
				startingRef.current = false;
				setRequesting(false);
				setSourcePhase(null);
				setError('当前浏览器不支持录音');
				setPhase('error');
				return;
			}
			if (typeof MediaRecorder === 'undefined') {
				startingRef.current = false;
				setRequesting(false);
				setSourcePhase(null);
				setError('当前浏览器不支持录音');
				setPhase('error');
				return;
			}

			void (async () => {
				try {
					const stream = await navigator.mediaDevices.getUserMedia({
						audio: {
							channelCount: 1,
							echoCancellation: true,
							noiseSuppression: true,
							autoGainControl: true,
						},
					});
					// Released while the permission prompt was open: never start later.
					if (!mountedRef.current || token !== startTokenRef.current) {
						stream.getTracks().forEach((track) => track.stop());
						return;
					}
					streamRef.current = stream;
					const mimeType = [
						'audio/webm;codecs=opus',
						'audio/webm',
						'audio/mp4',
					].find((type) => MediaRecorder.isTypeSupported(type));
					const recorder = mimeType
						? new MediaRecorder(stream, { mimeType })
						: new MediaRecorder(stream);
					recorderRef.current = recorder;
					chunksRef.current = [];

					recorder.ondataavailable = (event) => {
						if (event.data && event.data.size > 0) chunksRef.current.push(event.data);
					};
					recorder.onerror = () => {
						cleanup();
						if (!mountedRef.current) return;
						setError('识别失败');
						setPhase('error');
						setSourcePhase(null);
						setRequesting(false);
					};
					recorder.onstop = () => {
						const chunks = chunksRef.current;
						const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
						chunksRef.current = [];
						releaseMic();
						recorderRef.current = null;
						if (!mountedRef.current) return;
						if (discardRef.current) {
							discardRef.current = false;
							setPhase('idle');
							setSourcePhase(null);
							setRequesting(false);
							return;
						}
						if (blob.size < 500) {
							setError('没有识别到语音');
							setPhase('error');
							setSourcePhase(null);
							setRequesting(false);
							return;
						}
						if (blob.size > MAX_UPLOAD_BYTES) {
							setError('录音文件超过大小限制');
							setPhase('error');
							setSourcePhase(null);
							setRequesting(false);
							return;
						}
						lastBlobRef.current = blob;
						void transcribe(blob, recordingSessionRef.current);
					};

					recorder.start(250);
					startingRef.current = false;
					setRequesting(false);
					startedAtRef.current = performance.now();
					setSeconds(0);
					setPhase('recording');
					clearTimers();
					timerRef.current = window.setInterval(() => {
						const elapsed = Math.floor((performance.now() - startedAtRef.current) / 1000);
						setSeconds(elapsed);
					}, 250);
					autoStopRef.current = window.setTimeout(() => {
						if (stateRef.current === 'recording' && recorder.state !== 'inactive') {
							clearTimers();
							setPhase('transcribing');
							setSourcePhase(null);
							releaseMic();
							try {
								recorder.stop();
							} catch {
								cleanup();
								setError('识别失败');
								setPhase('error');
							}
						}
					}, maxSeconds * 1000);
				} catch (err) {
					if (token !== startTokenRef.current || !mountedRef.current) return;
					startingRef.current = false;
					setRequesting(false);
					cleanup();
					setSourcePhase(null);
					setError(mapMicrophoneError(err));
					setPhase('error');
				}
			})();
		},
		[cleanup, clearTimers, enabled, maxSeconds, releaseMic, setPhase, setSourcePhase, transcribe],
	);

	const cancel = useCallback(() => {
		// Invalidates a pending getUserMedia and any in-flight transcription.
		startTokenRef.current += 1;
		startingRef.current = false;
		transcribeTokenRef.current += 1;
		transcribeAbortRef.current?.abort();
		transcribeAbortRef.current = null;
		discardRef.current = true;
		const recorder = recorderRef.current;
		clearTimers();
		releaseMic();
		if (recorder && recorder.state !== 'inactive') {
			try {
				recorder.stop();
			} catch {
				/* already stopping */
			}
		}
		recorderRef.current = null;
		chunksRef.current = [];
		if (mountedRef.current) {
			setRequesting(false);
			setSourcePhase(null);
			setSeconds(0);
			setError(null);
			setPhase('idle');
		}
	}, [clearTimers, releaseMic, setPhase, setSourcePhase]);

	const stop = useCallback(() => {
		// Releasing the push-to-talk key before permission resolves must cancel,
		// not start a delayed recording.
		if (startingRef.current) {
			cancel();
			return;
		}
		const recorder = recorderRef.current;
		if (!recorder || recorder.state === 'inactive') return;
		clearTimers();
		setPhase('transcribing');
		setSourcePhase(null);
		// Release the physical microphone immediately; the buffered recorder
		// still emits its final data / stop event.
		releaseMic();
		try {
			recorder.stop();
		} catch {
			cleanup();
			setPhase('error');
			setError('识别失败');
		}
	}, [cancel, cleanup, clearTimers, releaseMic, setPhase, setSourcePhase]);

	const retry = useCallback(() => {
		if (stateRef.current === 'transcribing' || startingRef.current) return;
		if (lastBlobRef.current) {
			void transcribe(lastBlobRef.current, sessionKeyRef.current);
		} else {
			start('button');
		}
	}, [start, transcribe]);

	const toggle = useCallback(() => {
		if (startingRef.current) {
			cancel();
			return;
		}
		if (stateRef.current === 'recording') {
			stop();
			return;
		}
		if (stateRef.current === 'transcribing') return;
		if (stateRef.current === 'error' && lastBlobRef.current) {
			retry();
			return;
		}
		start('button');
	}, [cancel, retry, start, stop]);

	useEffect(() => {
		mountedRef.current = true;
		return () => {
			mountedRef.current = false;
			startTokenRef.current += 1;
			transcribeTokenRef.current += 1;
			transcribeAbortRef.current?.abort();
			transcribeAbortRef.current = null;
			clearTimers();
			discardRef.current = true;
			const recorder = recorderRef.current;
			if (recorder && recorder.state !== 'inactive') {
				try {
					recorder.stop();
				} catch {
					/* ignore */
				}
			}
			recorderRef.current = null;
			releaseMic();
		};
	}, [clearTimers, releaseMic]);

	// A transcript and its retry buffer belong to the session that started recording.
	useEffect(() => {
		const previous = recordingSessionRef.current;
		if (!previous || previous === sessionKey) return;
		if (stateRef.current === 'recording' || startingRef.current) cancel();
		// Drop a retry buffer that belongs to the old conversation.
		lastBlobRef.current = null;
	}, [cancel, sessionKey]);

	return { state, seconds, error, source, requesting, start, stop, cancel, toggle, retry };
}
