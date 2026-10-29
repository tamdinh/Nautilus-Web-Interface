import { useCallback, useEffect, useState } from 'react';
import api from '@/lib/api';
import { useNotification } from '@/contexts/NotificationContext';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog';

interface TradingState {
  mode: 'paper' | 'live';
  trading_enabled: boolean;
  cancel_pending?: boolean;
  ready_for_live?: boolean;
  problems?: string[];
}

const POLL_MS = 5000;

/** Always-visible environment banner (paper / live) with an emergency kill switch. */
export default function TradingStatusBar() {
  const { success, error } = useNotification();
  const [state, setState] = useState<TradingState | null>(null);
  const [stale, setStale] = useState(false);
  const [killing, setKilling] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setState(await api.get<TradingState>('/api/trading/state', { retries: 0 }));
      setStale(false);
    } catch {
      setStale(true);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  const kill = async () => {
    setKilling(true);
    try {
      await api.post('/api/trading/kill', { reason: 'manual kill switch (UI)', cancel_open: true });
      success('Kill switch activated: live trading disarmed, open orders being cancelled.');
    } catch (e) {
      error(`Kill switch request failed: ${e instanceof Error ? e.message : 'unknown error'}. Check the exchange directly.`);
    } finally {
      setKilling(false);
      refresh();
    }
  };

  if (!state && !stale) return null;

  const live = state?.mode === 'live';
  const armed = live && state?.trading_enabled;
  const tone = stale
    ? 'bg-yellow-500 text-black'
    : armed
      ? 'bg-red-700 text-white'
      : live
        ? 'bg-orange-500 text-black'
        : 'bg-blue-700 text-white';
  const label = stale
    ? 'TRADING STATE UNKNOWN: cannot reach backend'
    : armed
      ? 'LIVE TRADING ARMED: real orders are sent to the exchange'
      : live
        ? 'LIVE mode, DISARMED: orders are rejected'
        : 'PAPER mode: simulated orders only';

  return (
    <div
      role="status"
      className={`sticky top-0 z-40 flex items-center justify-between gap-3 px-4 py-1.5 text-xs font-semibold ${tone}`}
    >
      <span>
        {label}
        {state?.cancel_pending ? ' (cancelling late orders)' : ''}
      </span>
      {(live || stale) && (
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <button
              disabled={killing}
              className="rounded bg-black/80 px-3 py-1 text-white hover:bg-black disabled:opacity-50"
            >
              {killing ? 'Killing…' : 'KILL SWITCH'}
            </button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Activate kill switch?</AlertDialogTitle>
              <AlertDialogDescription>
                This disarms live trading immediately and cancels open exchange orders. It does not close positions.
                Re-arming needs an admin and a TOTP code.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction onClick={kill}>Kill trading</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </div>
  );
}
