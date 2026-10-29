import { useCallback, useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
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
  step_up_required?: boolean;
}

const POLL_MS = 5000;

function isAdmin(): boolean {
  try {
    return localStorage.getItem('nautilus_role') === 'admin';
  } catch {
    return false;
  }
}

/** Admin-only dialog: switch paper/live and arm live trading (backend enforces admin + TOTP + readiness). */
function TradingControls({ state, onChanged }: { state: TradingState; onChanged: () => void }) {
  const { success, error } = useNotification();
  const [open, setOpen] = useState(false);
  const [totp, setTotp] = useState('');
  const [busy, setBusy] = useState(false);
  const live = state.mode === 'live';
  const needCode = state.step_up_required !== false;

  const run = async (path: string, body: Record<string, unknown>, ok: string) => {
    setBusy(true);
    try {
      await api.post(path, { ...body, ...(needCode ? { totp_code: totp } : {}) }, { retries: 0 });
      success(ok);
      setTotp('');
      setOpen(false);
    } catch (e) {
      error(e instanceof Error ? e.message : 'Request failed');
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  const codeOk = !needCode || /^\d{6}$/.test(totp);

  return (
    <AlertDialog open={open} onOpenChange={(v) => { setOpen(v); if (!v) setTotp(''); }}>
      <AlertDialogTrigger asChild>
        <button className="rounded bg-white/20 px-3 py-1 hover:bg-white/30">Trading controls</button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Trading controls</AlertDialogTitle>
          <AlertDialogDescription>
            Mode: <b>{state.mode}</b>, live orders {state.trading_enabled ? 'ARMED' : 'disarmed'}. Entering live mode
            always starts disarmed; arming is a separate step.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {!!state.problems?.length && (
          <ul className="list-disc pl-5 text-sm text-red-700">
            {state.problems.map((p) => <li key={p}>{p}</li>)}
          </ul>
        )}
        {needCode && (
          <input
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            value={totp}
            onChange={(e) => setTotp(e.target.value.replace(/\D/g, ''))}
            placeholder="6-digit authenticator code"
            className="w-full rounded border px-3 py-2 text-sm"
          />
        )}
        <div className="flex flex-wrap gap-2">
          {!live && (
            <Button
              variant="destructive"
              disabled={busy || !codeOk || state.ready_for_live === false}
              onClick={() => run('/api/trading/mode', { mode: 'live', reason: 'switch to live (UI)' }, 'Live mode enabled (disarmed).')}
            >
              Switch to LIVE
            </Button>
          )}
          {live && !state.trading_enabled && (
            <Button
              variant="destructive"
              disabled={busy || !codeOk || state.ready_for_live === false}
              onClick={() => run('/api/trading/arm', { reason: 'armed (UI)' }, 'Live trading armed.')}
            >
              ARM live trading
            </Button>
          )}
          {live && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => run('/api/trading/mode', { mode: 'paper', reason: 'switch to paper (UI)' }, 'Paper mode enabled.')}
            >
              Switch to PAPER
            </Button>
          )}
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel>Close</AlertDialogCancel>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

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
      <div className="flex items-center gap-2">
        {!stale && state && isAdmin() && <TradingControls state={state} onChanged={refresh} />}
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
    </div>
  );
}
