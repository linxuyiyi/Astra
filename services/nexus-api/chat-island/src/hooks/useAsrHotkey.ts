import { useEffect, useRef } from 'react';
import {
	matchesShortcut,
	releasedShortcutPart,
	type AsrShortcut,
} from '@/lib/asrShortcut.ts';

interface UseAsrHotkeyOptions {
	shortcut: AsrShortcut;
	enabled: boolean;
	/** Recording can only begin from idle/error, not while busy. */
	canStart: boolean;
	/** Covers button-started recordings too, not just an active PTT press. */
	recording: boolean;
	onStart: () => void;
	onStop: () => void;
	onCancel: () => void;
}

/**
 * Push-to-talk key handling for the chat input.
 *
 * Only a complete shortcut starts recording. The keydown and keyup listeners
 * live on the window while the chat island is mounted; there is no OS-level
 * registration. Releasing any required part of the combination ends the
 * recording, so either key may be released first.
 */
export function useAsrHotkey({
	shortcut,
	enabled,
	canStart,
	recording,
	onStart,
	onStop,
	onCancel,
}: UseAsrHotkeyOptions): void {
	const activeRef = useRef(false);
	const canStartRef = useRef(canStart);
	const recordingRef = useRef(recording);
	const composingRef = useRef(false);
	const onStartRef = useRef(onStart);
	const onStopRef = useRef(onStop);
	const onCancelRef = useRef(onCancel);

	canStartRef.current = canStart;
	recordingRef.current = recording;
	onStartRef.current = onStart;
	onStopRef.current = onStop;
	onCancelRef.current = onCancel;

	useEffect(() => {
		if (!enabled) return;
		const cancelActive = () => {
			if (!activeRef.current && !recordingRef.current) return;
			activeRef.current = false;
			onCancelRef.current();
		};

		const handleKeyDown = (event: KeyboardEvent) => {
			if (event.isComposing || event.keyCode === 229 || composingRef.current) return;
			if (event.code === 'Escape' && (activeRef.current || recordingRef.current)) {
				event.preventDefault();
				event.stopPropagation();
				cancelActive();
				return;
			}
			if (!matchesShortcut(event, shortcut)) return;
			// Stop the dead-key/character default. On macOS Option+K otherwise
			// produces `˚` in the focused textarea.
			event.preventDefault();
			event.stopPropagation();
			if (event.repeat || activeRef.current) return;
			if (!canStartRef.current) return;
			activeRef.current = true;
			onStartRef.current();
		};

		const handleKeyUp = (event: KeyboardEvent) => {
			if (!activeRef.current) return;
			if (!releasedShortcutPart(event, shortcut)) return;
			event.preventDefault();
			event.stopPropagation();
			activeRef.current = false;
			onStopRef.current();
		};

		const handleCompositionStart = () => {
			composingRef.current = true;
		};
		const handleCompositionEnd = () => {
			composingRef.current = false;
		};
		const handleVisibility = () => {
			if (document.visibilityState === 'hidden') cancelActive();
		};

		window.addEventListener('keydown', handleKeyDown, true);
		window.addEventListener('keyup', handleKeyUp, true);
		window.addEventListener('blur', cancelActive);
		window.addEventListener('pagehide', cancelActive);
		document.addEventListener('visibilitychange', handleVisibility);
		document.addEventListener('compositionstart', handleCompositionStart, true);
		document.addEventListener('compositionend', handleCompositionEnd, true);

		return () => {
			window.removeEventListener('keydown', handleKeyDown, true);
			window.removeEventListener('keyup', handleKeyUp, true);
			window.removeEventListener('blur', cancelActive);
			window.removeEventListener('pagehide', cancelActive);
			document.removeEventListener('visibilitychange', handleVisibility);
			document.removeEventListener('compositionstart', handleCompositionStart, true);
			document.removeEventListener('compositionend', handleCompositionEnd, true);
			cancelActive();
		};
	}, [enabled, shortcut]);
}
