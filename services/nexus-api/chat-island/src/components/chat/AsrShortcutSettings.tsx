import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { XIcon } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Kbd } from '@/components/ui/kbd';
import { useTranslation } from '@/i18n/useI18n.ts';
import {
	DEFAULT_ASR_SHORTCUT,
	formatShortcut,
	formatShortcutName,
	shortcutFromKeyboardEvent,
	validateAsrShortcut,
	type AsrShortcut,
} from '@/lib/asrShortcut.ts';

interface AsrShortcutSettingsProps {
	open: boolean;
	shortcut: AsrShortcut;
	onClose: () => void;
	onSave: (shortcut: AsrShortcut) => void;
}

function validationMessage(
	reason: 'invalid' | 'modifier' | 'forbidden' | 'reserved',
	t: (key: string) => string,
): string {
	if (reason === 'modifier') return t('asr.pttNeedsModifier');
	if (reason === 'forbidden') return t('asr.pttForbidden');
	if (reason === 'reserved') return t('asr.pttReserved');
	return t('asr.pttInvalid');
}

/**
 * Small local preference editor for the push-to-talk shortcut. It deliberately
 * stays inside the chat island: no new server setting and no OS-level binding.
 */
export function AsrShortcutSettings({
	open,
	shortcut,
	onClose,
	onSave,
}: AsrShortcutSettingsProps) {
	const { t } = useTranslation();
	const [draft, setDraft] = useState<AsrShortcut>(shortcut);
	const [error, setError] = useState('');
	const captureRef = useRef<HTMLDivElement>(null);

	useEffect(() => {
		if (!open) return;
		setDraft(shortcut);
		setError('');
		const frame = window.requestAnimationFrame(() => captureRef.current?.focus());
		return () => window.cancelAnimationFrame(frame);
	}, [open, shortcut]);

	const handleKeyDown = useCallback(
		(event: KeyboardEvent) => {
			if (!open) return;
			// Let plain Tab move focus inside the dialog; a Tab shortcut itself is
			// forbidden and cannot be captured.
			if (event.code === 'Tab' && !event.altKey && !event.ctrlKey && !event.metaKey) {
				return;
			}
			event.preventDefault();
			event.stopPropagation();
			if (event.code === 'Escape') {
				onClose();
				return;
			}
			const next = shortcutFromKeyboardEvent(event);
			if (!next) return;
			setDraft(next);
			const result = validateAsrShortcut(next);
			setError(result.ok ? '' : validationMessage(result.reason, t));
		},
		[onClose, open, t],
	);

	useEffect(() => {
		if (!open) return;
		window.addEventListener('keydown', handleKeyDown, true);
		return () => window.removeEventListener('keydown', handleKeyDown, true);
	}, [handleKeyDown, open]);

	if (!open) return null;

	const result = validateAsrShortcut(draft);
	const save = () => {
		const current = validateAsrShortcut(draft);
		if (!current.ok) {
			setError(validationMessage(current.reason, t));
			return;
		}
		onSave(draft);
		onClose();
	};

	return createPortal(
		<div
			className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4"
			onMouseDown={(event) => {
				if (event.target === event.currentTarget) onClose();
			}}
		>
			<div
				role="dialog"
				aria-modal="true"
				aria-labelledby="asr-shortcut-title"
				data-testid="asr-shortcut-dialog"
				className="w-full max-w-md rounded-2xl border bg-background p-5 shadow-xl"
			>
				<div className="flex items-start justify-between gap-4">
					<div>
						<h2 id="asr-shortcut-title" className="text-base font-semibold">
							{t('asr.pttSettingsTitle')}
						</h2>
						<p className="mt-1 text-xs leading-5 text-muted-foreground">
							{t('asr.pttSettingsHelp')}
						</p>
					</div>
					<Button
						type="button"
						variant="ghost"
						size="icon"
						aria-label={t('asr.pttClose')}
						onClick={onClose}
					>
						<XIcon className="size-4" />
					</Button>
				</div>

				<div className="mt-4 rounded-xl border border-dashed bg-muted/30 px-4 py-5 text-center">
					<div className="text-xs text-muted-foreground">{t('asr.pttCurrent')}</div>
					<div className="mt-2 flex items-center justify-center gap-2">
						<Kbd className="h-8 min-w-8 px-2 text-sm">{formatShortcut(draft)}</Kbd>
						<span className="text-xs text-muted-foreground">
							{formatShortcutName(draft)}
						</span>
					</div>
				</div>

				<div
					ref={captureRef}
					tabIndex={-1}
					data-testid="asr-shortcut-capture"
					className="mt-3 rounded-xl border bg-background px-4 py-4 text-center text-sm outline-none focus:ring-2 focus:ring-primary/30"
				>
					<div className="font-medium">{t('asr.pttCapture')}</div>
					<div className="mt-1 text-xs text-muted-foreground">{t('asr.pttCaptureHint')}</div>
				</div>

				{error && (
					<p role="alert" className="mt-3 text-xs text-destructive">
						{error}
					</p>
				)}

				<div className="mt-5 flex items-center justify-between gap-2">
					<Button
						type="button"
						variant="ghost"
						size="sm"
						data-testid="asr-shortcut-reset"
						onClick={() => {
							setDraft(DEFAULT_ASR_SHORTCUT);
							setError('');
						}}
					>
						{t('asr.pttReset')}
					</Button>
					<div className="flex items-center gap-2">
						<Button type="button" variant="ghost" size="sm" onClick={onClose}>
							{t('common.cancel')}
						</Button>
						<Button
							type="button"
							size="sm"
							data-testid="asr-shortcut-save"
							disabled={!result.ok}
							onClick={save}
						>
							{t('asr.pttSave')}
						</Button>
					</div>
				</div>
			</div>
		</div>,
		document.body,
	);
}
