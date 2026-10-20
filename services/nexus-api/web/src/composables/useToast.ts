import { ElNotification } from 'element-plus'

export type ToastKind = 'success' | 'error' | 'info'

// Single application-wide feedback channel built on the native Element Plus
// notification component. Business code calls notify() instead of window.alert;
// failures announce as error notifications and never block the user's input.
export function notify(message: string, kind: ToastKind = 'success', duration = 2600): void {
  ElNotification({
    message,
    type: kind,
    duration,
    position: 'bottom-right',
    offset: 24,
  })
}
