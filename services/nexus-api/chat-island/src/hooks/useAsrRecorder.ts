import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from '@/i18n/useI18n.ts';

export type AsrRecorderState = 'idle' | 'recording' | 'transcribing' | 'error';

interface UseAsrRecorderOptions {
	enabled?: boolean;
	maxSeconds?: number;
	onText: (text: string) => void;
}

interface AsrRecorder {
	state: AsrRecorderState;
	seconds: number;
	error: string | null;
	toggle: () => void;
	retry: () => void;
}

const MAX_UPLOAD_BYTES = 12 * 1024 * 1024;
const ASR_ENDPOINT = '/api/asr/transcribe';

/**
 * Browser recording -> POST /api/asr/transcribe -> text.
 *
 * Deliberately not a streaming implementation: the server recognises the
 * completed recording. The microphone is released as soon as recording stops;
 * no track is kept alive between rounds.
 */
export function useAsrRecorder({
	enabled = true,
	maxSeconds = 60,
	onText,
}: UseAsrRecorderOptions): AsrRecorder {
	const { t } = useTranslation();
	const [state, setState] = useState<AsrRecorderState>('idle');
	const [seconds, setSeconds] = useState(0);
	const [error, setError] = useState<string | null>(null);

	const stateRef = useRef<AsrRecorderState>('idle');
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

	onTextRef.current = onText;

	const setPhase = useCallback((next: AsrRecorderState) => {
		stateRef.current = next;
		setState(next);
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

	const errorMessage = useCallback(
		(code: string, status: number): string => {
			const map: Record<string, string> = {
				rate_limited: t('asr.rateLimited'),
				server_busy: t('asr.busy'),
				audio_too_long: t('asr.tooLong'),
				unsupported_audio: t('asr.unsupportedFormat'),
				empty_audio: t('asr.empty'),
				recognition_timeout: t('asr.timeout'),
				payload_too_large: t('asr.tooLarge'),
				model_not_ready: t('asr.notReady'),
			};
			return map[code] || (status ? t('asr.failedWithStatus', { status }) : t('asr.failed'));
		},
		[t],
	);

	const transcribe = useCallback(
		async (blob: Blob) => {
			setError(null);
			setPhase('transcribing');
			const form = new FormData();
			const ext = blob.type.includes('mp4') ? 'm4a' : blob.type.includes('ogg') ? 'ogg' : 'webm';
			form.append('file', blob, `recording.${ext}`);

			const controller = new AbortController();
			const timeout = window.setTimeout(() => controller.abort(), 60_000);
			try {
				const response = await fetch(ASR_ENDPOINT, {
					method: 'POST',
					body: form,
					signal: controller.signal,
				});
				const body = await response.json().catch(() => ({}));
				if (!response.ok) {
					const detail = body?.detail;
					const code = typeof detail === 'object' ? detail?.code : '';
					throw new Error(errorMessage(code || '', response.status));
				}
				const text = String(body?.text || '').trim();
				if (!text) throw new Error(t('asr.empty'));
				if (mountedRef.current) {
					onTextRef.current(text);
					setPhase('idle');
				}
			} catch (err) {
				if (!mountedRef.current) return;
				const message =
					err instanceof DOMException && err.name === 'AbortError'
						? t('asr.timeout')
						: err instanceof Error
							? err.message
							: t('asr.failed');
				setError(message);
				setPhase('error');
			} finally {
				window.clearTimeout(timeout);
			}
		},
		[errorMessage, setPhase, t],
	);

	const start = useCallback(async () => {
		if (
			!enabled ||
			startingRef.current ||
			stateRef.current === 'recording' ||
			stateRef.current === 'transcribing'
		) {
			return;
		}
		startingRef.current = true;
		if (!navigator.mediaDevices?.getUserMedia) {
			setError(t('asr.unsupported'));
			setPhase('error');
			return;
		}
		if (typeof MediaRecorder === 'undefined') {
			setError(t('asr.unsupported'));
			setPhase('error');
			return;
		}

		setError(null);
		// A new recording replaces the retry buffer; an old clip must never be
		// re-uploaded after a permission error.
		lastBlobRef.current = null;
		try {
			const stream = await navigator.mediaDevices.getUserMedia({
				audio: {
					channelCount: 1,
					echoCancellation: true,
					noiseSuppression: true,
					autoGainControl: true,
				},
			});
			if (!mountedRef.current) {
				startingRef.current = false;
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
				setError(t('asr.failed'));
				setPhase('error');
			};
			recorder.onstop = () => {
				const chunks = chunksRef.current;
				const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
				chunksRef.current = [];
				releaseMic();
				recorderRef.current = null;
				if (!mountedRef.current) return;
				if (blob.size < 500) {
					setError(t('asr.empty'));
					setPhase('error');
					return;
				}
				if (blob.size > MAX_UPLOAD_BYTES) {
					setError(t('asr.tooLarge'));
					setPhase('error');
					return;
				}
				lastBlobRef.current = blob;
				void transcribe(blob);
			};

			recorder.start(250);
			startingRef.current = false;
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
					releaseMic();
					recorder.stop();
				}
			}, maxSeconds * 1000);
		} catch (err) {
			startingRef.current = false;
			cleanup();
			if (!mountedRef.current) return;
			const name = err instanceof DOMException ? err.name : '';
			setError(
				name === 'NotAllowedError' || name === 'SecurityError'
					? t('asr.permission')
					: name === 'NotFoundError'
						? t('asr.noDevice')
						: err instanceof Error
							? err.message
							: t('asr.unsupported'),
			);
			setPhase('error');
		}
	}, [cleanup, clearTimers, enabled, maxSeconds, releaseMic, setPhase, t, transcribe]);

	const stop = useCallback(() => {
		const recorder = recorderRef.current;
		if (!recorder || recorder.state === 'inactive') return;
		clearTimers();
		setPhase('transcribing');
		// Release the physical microphone immediately; the buffered recorder
		// still emits its final data / stop event.
		releaseMic();
		try {
			recorder.stop();
		} catch {
			cleanup();
			setPhase('error');
			setError(t('asr.failed'));
		}
	}, [cleanup, clearTimers, releaseMic, setPhase, t]);

	const retry = useCallback(() => {
		if (stateRef.current === 'transcribing') return;
		if (lastBlobRef.current) {
			void transcribe(lastBlobRef.current);
		} else {
			void start();
		}
	}, [start, transcribe]);

	const toggle = useCallback(() => {
		if (stateRef.current === 'recording') {
			stop();
			return;
		}
		if (stateRef.current === 'transcribing') return;
		if (stateRef.current === 'error' && lastBlobRef.current) {
			retry();
			return;
		}
		void start();
	}, [retry, start, stop]);

	useEffect(() => {
		mountedRef.current = true;
		return () => {
			mountedRef.current = false;
			clearTimers();
			const recorder = recorderRef.current;
			if (recorder && recorder.state !== 'inactive') {
				try {
					recorder.stop();
				} catch {
					/* ignore */
				}
			}
			releaseMic();
		};
	}, [clearTimers, releaseMic]);

	return { state, seconds, error, toggle, retry };
}
