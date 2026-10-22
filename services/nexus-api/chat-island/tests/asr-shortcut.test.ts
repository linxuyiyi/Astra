import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	ASR_SHORTCUT_STORAGE_KEY,
	DEFAULT_ASR_SHORTCUT,
	formatShortcut,
	formatShortcutName,
	loadAsrShortcut,
	releasedShortcutPart,
	saveAsrShortcut,
	shortcutFromKeyboardEvent,
	validateAsrShortcut,
} from '../src/lib/asrShortcut.ts';

function storage() {
	const values = new Map<string, string>();
	return {
		getItem: (key: string) => values.get(key) ?? null,
		setItem: (key: string, value: string) => { values.set(key, value); },
	};
}

function keyEvent(code: string, init: KeyboardEventInit = {}): KeyboardEvent {
	return { code, key: '', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, isComposing: false, keyCode: 0, ...init } as KeyboardEvent;
}

test('默认 Alt+K 在 Mac 显示 Option 风格，在其他平台显示 Alt', () => {
	assert.equal(formatShortcut(DEFAULT_ASR_SHORTCUT, 'MacIntel'), '⌥ K');
	assert.equal(formatShortcutName(DEFAULT_ASR_SHORTCUT, 'MacIntel'), 'Option + K');
	assert.equal(formatShortcut(DEFAULT_ASR_SHORTCUT, 'Win32'), 'Alt + K');
	assert.equal(formatShortcutName(DEFAULT_ASR_SHORTCUT, 'Linux arm64'), 'Alt + K');
});

test('只接受非修饰键，Space/Enter/Esc 不能作为快捷键', () => {
	assert.equal(shortcutFromKeyboardEvent(keyEvent('AltLeft', { altKey: true })), null);
	const space = shortcutFromKeyboardEvent(keyEvent('Space', { altKey: true, key: ' ' }));
	assert.ok(space);
	assert.equal(validateAsrShortcut(space!).ok, false);
	assert.equal(shortcutFromKeyboardEvent(keyEvent('Enter', { altKey: true, key: 'Enter' }))?.code, 'Enter');
	assert.equal(shortcutFromKeyboardEvent(keyEvent('Escape', { altKey: true, key: 'Escape' }))?.code, 'Escape');
	const combo = shortcutFromKeyboardEvent(keyEvent('KeyJ', { altKey: true, key: 'j' }));
	assert.deepEqual(combo, { code: 'KeyJ', alt: true, ctrl: false, meta: false, shift: false });
});

test('校验必须包含 Alt/Ctrl/Command，并拒绝系统保留组合', () => {
	assert.deepEqual(validateAsrShortcut({ code: 'KeyK', alt: false, ctrl: false, meta: false, shift: true }), { ok: false, reason: 'modifier' });
	assert.deepEqual(validateAsrShortcut({ code: 'Space', alt: true, ctrl: false, meta: false, shift: false }), { ok: false, reason: 'forbidden' });
	assert.deepEqual(validateAsrShortcut({ code: 'Enter', alt: true, ctrl: false, meta: false, shift: false }), { ok: false, reason: 'forbidden' });
	assert.deepEqual(validateAsrShortcut({ code: 'Tab', alt: true, ctrl: false, meta: false, shift: false }), { ok: false, reason: 'forbidden' });
	assert.deepEqual(validateAsrShortcut({ code: 'ArrowUp', alt: true, ctrl: false, meta: false, shift: false }), { ok: false, reason: 'forbidden' });
	assert.deepEqual(validateAsrShortcut({ code: 'KeyQ', alt: false, ctrl: false, meta: true, shift: false }), { ok: false, reason: 'reserved' });
	assert.deepEqual(validateAsrShortcut({ code: 'KeyK', alt: true, ctrl: false, meta: false, shift: false }), { ok: true });
});

test('释放任意一个组合键都结束录音', () => {
	const shortcut = DEFAULT_ASR_SHORTCUT;
	assert.equal(releasedShortcutPart(keyEvent('KeyK', { altKey: true }), shortcut), true);
	assert.equal(releasedShortcutPart(keyEvent('AltLeft', { altKey: false }), shortcut), true);
	assert.equal(releasedShortcutPart(keyEvent('ShiftLeft', { altKey: true, shiftKey: false }), shortcut), false);
});

test('偏好持久化损坏时回退默认，保存后可重新读取', () => {
	const store = storage();
	assert.deepEqual(loadAsrShortcut(store), DEFAULT_ASR_SHORTCUT);
	store.setItem(ASR_SHORTCUT_STORAGE_KEY, '{');
	assert.deepEqual(loadAsrShortcut(store), DEFAULT_ASR_SHORTCUT);
	const custom = { code: 'KeyJ', alt: true, ctrl: false, meta: false, shift: false };
	saveAsrShortcut(custom, store);
	assert.deepEqual(loadAsrShortcut(store), custom);
});
