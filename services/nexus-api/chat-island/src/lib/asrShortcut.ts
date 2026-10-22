/**
 * Lightweight local preference for the push-to-talk shortcut.
 *
 * The shortcut is a browser-page shortcut: it is registered by the chat island
 * only while that page is mounted. Nothing is installed at the OS level.
 */

export interface AsrShortcut {
	code: string;
	alt: boolean;
	ctrl: boolean;
	meta: boolean;
	shift: boolean;
}

export type ShortcutValidation =
	| { ok: true }
	| { ok: false; reason: 'invalid' | 'modifier' | 'forbidden' | 'reserved' };

export const ASR_SHORTCUT_STORAGE_KEY = 'nexus:asr:shortcut:v1';

export const DEFAULT_ASR_SHORTCUT: AsrShortcut = {
	code: 'KeyK',
	alt: true,
	ctrl: false,
	meta: false,
	shift: false,
};

const MODIFIER_CODES = new Set([
	'AltLeft',
	'AltRight',
	'ControlLeft',
	'ControlRight',
	'MetaLeft',
	'MetaRight',
	'ShiftLeft',
	'ShiftRight',
]);

const FORBIDDEN_CODES = new Set([
	'Space',
	'Enter',
	'NumpadEnter',
	'Escape',
	'Tab',
	'Backspace',
	'Delete',
	'Insert',
	'ArrowUp',
	'ArrowDown',
	'ArrowLeft',
	'ArrowRight',
	'Home',
	'End',
	'PageUp',
	'PageDown',
	'CapsLock',
	'ContextMenu',
]);

/** Browser/OS shortcuts that the page cannot reliably prevent or must not take. */
const RESERVED_COMBINATIONS = new Set([
	'meta+Space',
	'ctrl+Space',
	'alt+Tab',
	'meta+Tab',
	'ctrl+alt+Delete',
	'alt+F4',
	'alt+ArrowLeft',
	'alt+ArrowRight',
	'alt+KeyD',
	'ctrl+KeyL',
	'ctrl+KeyT',
	'ctrl+KeyW',
	'ctrl+KeyN',
	'ctrl+shift+KeyT',
	'ctrl+shift+KeyN',
	'ctrl+shift+Escape',
	'meta+KeyQ',
	'meta+KeyW',
	'meta+KeyR',
	'meta+KeyL',
	'meta+KeyT',
	'meta+KeyN',
	'meta+KeyH',
	'meta+KeyM',
	'meta+shift+Digit3',
	'meta+shift+Digit4',
	'meta+shift+Digit5',
	'F11',
]);

function isMacPlatform(platform: string): boolean {
	return /mac|iphone|ipad|ipod/i.test(platform);
}

export function isMac(platform = typeof navigator === 'undefined' ? '' : navigator.platform): boolean {
	return isMacPlatform(platform);
}

function normalize(value: unknown): AsrShortcut | null {
	if (!value || typeof value !== 'object') return null;
	const item = value as Record<string, unknown>;
	if (typeof item.code !== 'string' || !item.code) return null;
	if (
		typeof item.alt !== 'boolean' ||
		typeof item.ctrl !== 'boolean' ||
		typeof item.meta !== 'boolean' ||
		typeof item.shift !== 'boolean'
	) {
		return null;
	}
	return {
		code: item.code,
		alt: item.alt,
		ctrl: item.ctrl,
		meta: item.meta,
		shift: item.shift,
	};
}

type ShortcutStorage = Pick<Storage, 'getItem' | 'setItem'>;

export function loadAsrShortcut(storage: ShortcutStorage | null = storageOrNull()): AsrShortcut {
	if (!storage) return DEFAULT_ASR_SHORTCUT;
	try {
		const parsed = normalize(JSON.parse(storage.getItem(ASR_SHORTCUT_STORAGE_KEY) || 'null'));
		if (!parsed) return DEFAULT_ASR_SHORTCUT;
		return validateAsrShortcut(parsed).ok ? parsed : DEFAULT_ASR_SHORTCUT;
	} catch {
		return DEFAULT_ASR_SHORTCUT;
	}
}

export function saveAsrShortcut(
	shortcut: AsrShortcut,
	storage: ShortcutStorage | null = storageOrNull(),
): void {
	if (!storage) return;
	try {
		storage.setItem(ASR_SHORTCUT_STORAGE_KEY, JSON.stringify(shortcut));
	} catch {
		// Private mode/quota errors must not block the chat input.
	}
}

function storageOrNull(): ShortcutStorage | null {
	try {
		return typeof window === 'undefined' ? null : window.localStorage;
	} catch {
		return null;
	}
}

function keyLabel(code: string): string {
	if (/^Key[A-Z]$/.test(code)) return code.slice(3);
	if (/^Digit[0-9]$/.test(code)) return code.slice(5);
	if (/^F[0-9]{1,2}$/.test(code)) return code;
	const named: Record<string, string> = {
		Space: 'Space',
		Enter: 'Enter',
		NumpadEnter: 'Enter',
		Escape: 'Esc',
		Tab: 'Tab',
		Backspace: 'Backspace',
		Delete: 'Delete',
		Insert: 'Insert',
		ArrowUp: '↑',
		ArrowDown: '↓',
		ArrowLeft: '←',
		ArrowRight: '→',
		Home: 'Home',
		End: 'End',
		PageUp: 'PageUp',
		PageDown: 'PageDown',
		Minus: '-',
		Equal: '=',
		BracketLeft: '[',
		BracketRight: ']',
		Backslash: '\\',
		Semicolon: ';',
		Quote: "'",
		Comma: ',',
		Period: '.',
		Slash: '/',
		Backquote: '`',
	};
	return named[code] || code;
}

function modifierParts(shortcut: AsrShortcut, mac: boolean): string[] {
	const parts: string[] = [];
	if (shortcut.ctrl) parts.push(mac ? '⌃' : 'Ctrl');
	if (shortcut.alt) parts.push(mac ? '⌥' : 'Alt');
	if (shortcut.shift) parts.push(mac ? '⇧' : 'Shift');
	if (shortcut.meta) parts.push(mac ? '⌘' : 'Meta');
	return parts;
}

/** Compact label for the input hint, e.g. `⌥ K` or `Ctrl + Alt + K`. */
export function formatShortcut(
	shortcut: AsrShortcut,
	platform = typeof navigator === 'undefined' ? '' : navigator.platform,
): string {
	const mac = isMacPlatform(platform);
	const parts = modifierParts(shortcut, mac);
	const key = keyLabel(shortcut.code);
	return mac ? `${parts.join('')} ${key}` : `${parts.join(' + ')} + ${key}`;
}

/** Spoken/readable label for settings, e.g. `Option + K`. */
export function formatShortcutName(
	shortcut: AsrShortcut,
	platform = typeof navigator === 'undefined' ? '' : navigator.platform,
): string {
	const mac = isMacPlatform(platform);
	const parts: string[] = [];
	if (shortcut.ctrl) parts.push(mac ? 'Control' : 'Ctrl');
	if (shortcut.alt) parts.push(mac ? 'Option' : 'Alt');
	if (shortcut.shift) parts.push('Shift');
	if (shortcut.meta) parts.push(mac ? 'Command' : 'Meta');
	parts.push(keyLabel(shortcut.code));
	return parts.join(' + ');
}

export function shortcutFromKeyboardEvent(event: KeyboardEvent): AsrShortcut | null {
	if (event.isComposing || event.keyCode === 229) return null;
	if (!event.code || MODIFIER_CODES.has(event.code)) return null;
	// Forbidden keys are returned so the settings dialog can explain why they
	// cannot be used; push-to-talk matching never calls this function.
	return {
		code: event.code,
		alt: event.altKey,
		ctrl: event.ctrlKey,
		meta: event.metaKey,
		shift: event.shiftKey,
	};
}

export function matchesShortcut(event: KeyboardEvent, shortcut: AsrShortcut): boolean {
	if (event.isComposing || event.keyCode === 229) return false;
	return (
		event.code === shortcut.code &&
		event.altKey === shortcut.alt &&
		event.ctrlKey === shortcut.ctrl &&
		event.metaKey === shortcut.meta &&
		event.shiftKey === shortcut.shift
	);
}

export function releasedShortcutPart(event: KeyboardEvent, shortcut: AsrShortcut): boolean {
	if (event.code === shortcut.code) return true;
	return (
		(shortcut.alt && !event.altKey) ||
		(shortcut.ctrl && !event.ctrlKey) ||
		(shortcut.meta && !event.metaKey) ||
		(shortcut.shift && !event.shiftKey)
	);
}

export function validateAsrShortcut(shortcut: AsrShortcut): ShortcutValidation {
	if (!shortcut.code || MODIFIER_CODES.has(shortcut.code)) return { ok: false, reason: 'invalid' };
	if (FORBIDDEN_CODES.has(shortcut.code)) return { ok: false, reason: 'forbidden' };
	// Shift is a modifier for typing, not a safe push-to-talk modifier on its own.
	if (!shortcut.alt && !shortcut.ctrl && !shortcut.meta) {
		return { ok: false, reason: 'modifier' };
	}
	const signature = [
		shortcut.ctrl ? 'ctrl' : '',
		shortcut.alt ? 'alt' : '',
		shortcut.shift ? 'shift' : '',
		shortcut.meta ? 'meta' : '',
		shortcut.code,
	]
		.filter(Boolean)
		.join('+');
	if (RESERVED_COMBINATIONS.has(signature)) return { ok: false, reason: 'reserved' };
	return { ok: true };
}
