/**
 * Toasts.
 *
 * Reserved for things an operator must *see happen*: an action committed, a
 * cycle completed, a subsystem falling over. Routine polling never raises a
 * toast — a console that cries wolf during a quiet hour is ignored during a
 * flood.
 */

"use client";

import { useConsole } from "@/lib/store";

export function Toasts() {
  const toasts = useConsole((state) => state.toasts);
  const dismiss = useConsole((state) => state.dismissToast);

  if (!toasts.length) return null;

  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`toast ${toast.level}`}
          onClick={() => dismiss(toast.id)}
          title="Click to dismiss"
          role="alert"
        >
          <div className="toast-title">{toast.message}</div>
          {toast.detail ? <div className="toast-detail">{toast.detail}</div> : null}
        </div>
      ))}
    </div>
  );
}
