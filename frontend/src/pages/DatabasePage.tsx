import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useNotification } from "@/contexts/NotificationContext";
import api from "@/lib/api";

interface BackupFile {
  filename: string;
  size_mb: number;
  created_at: string;
}

interface OpResult {
  success?: boolean;
  message?: string;
}

/** SQLite database administration: backup, restore (TOTP step-up), optimize, clean. */
export default function DatabasePage() {
  const { success, error, warning } = useNotification();
  const [backups, setBackups] = useState<BackupFile[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [needCode, setNeedCode] = useState(true);
  const [restoreTarget, setRestoreTarget] = useState<string | null>(null);
  const [totp, setTotp] = useState("");

  const loadBackups = useCallback(async () => {
    try {
      const res = await api.get<{ backups: BackupFile[] }>("/api/database/backups");
      setBackups(res.backups ?? []);
    } catch (e) {
      error(e instanceof Error ? e.message : "Could not load backups");
    }
  }, [error]);

  useEffect(() => {
    loadBackups();
    api
      .get<{ step_up_required?: boolean }>("/api/trading/state")
      .then((s) => setNeedCode(s.step_up_required !== false))
      .catch(() => setNeedCode(true));
  }, [loadBackups]);

  // The backend reports most maintenance failures as HTTP 200 with success:false: check it.
  const run = async (name: string, path: string, body: Record<string, unknown>) => {
    setBusy(name);
    try {
      const res = await api.post<OpResult>(path, body, { retries: 0 });
      if (res.success === false) warning(res.message ?? `${name} failed`);
      else success(res.message ?? `${name} done`);
    } catch (e) {
      error(e instanceof Error ? e.message : `${name} failed`);
    } finally {
      setBusy(null);
      loadBackups();
    }
  };

  const restore = async () => {
    if (!restoreTarget) return;
    setBusy("restore");
    try {
      const res = await api.post<OpResult>(
        "/api/database/restore",
        { backup_file: restoreTarget, ...(needCode ? { totp_code: totp } : {}) },
        { retries: 0 },
      );
      success(`${res.message ?? "Restored"}. Trading is disarmed; re-arm it deliberately.`);
      setRestoreTarget(null);
      setTotp("");
    } catch (e) {
      error(e instanceof Error ? e.message : "Restore failed");
    } finally {
      setBusy(null);
      loadBackups();
    }
  };

  const codeOk = !needCode || /^\d{6}$/.test(totp);

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b">
        <div className="container mx-auto px-4 py-4 flex items-center justify-between">
          <h1 className="text-2xl font-bold">Database</h1>
          <Button variant="outline" onClick={() => (window.location.href = "/admin")}>
            ← Back to Dashboard
          </Button>
        </div>
      </header>

      <main className="container mx-auto px-4 py-8 space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Maintenance</CardTitle>
            <CardDescription>The system stores its data in a single SQLite database.</CardDescription>
          </CardHeader>
          <CardContent className="grid md:grid-cols-3 gap-4">
            <Button disabled={busy !== null} onClick={() => run("Backup", "/api/database/backup", { db_type: "all" })}>
              {busy === "Backup" ? "Backing up…" : "Create backup"}
            </Button>
            <Button
              variant="outline"
              disabled={busy !== null}
              onClick={() => run("Optimize", "/api/database/optimize", { db_type: "all" })}
            >
              {busy === "Optimize" ? "Optimizing…" : "Optimize (VACUUM + ANALYZE)"}
            </Button>
            <Button
              variant="outline"
              disabled={busy !== null}
              onClick={() => run("Clean", "/api/database/clean", { cache_type: "all" })}
            >
              {busy === "Clean" ? "Cleaning…" : "Clean old records (30+ days)"}
            </Button>
          </CardContent>
          <CardContent className="text-xs text-gray-500 pt-0">
            Clean removes triggered alerts and cancelled paper orders older than 30 days. Trade history and live
            orders are kept. Backups stay on the same volume: copy them off-host as well.
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Backups ({backups.length})</CardTitle>
            <CardDescription>Restoring replaces the current database and leaves live trading disarmed.</CardDescription>
          </CardHeader>
          <CardContent>
            {backups.length === 0 ? (
              <p className="text-sm text-gray-500">No backups yet.</p>
            ) : (
              <ul className="divide-y">
                {backups.map((b) => (
                  <li key={b.filename} className="flex items-center justify-between py-2 text-sm">
                    <span>
                      <span className="font-mono">{b.filename}</span>
                      <span className="ml-3 text-gray-500">
                        {b.size_mb} MB · {new Date(b.created_at).toLocaleString()}
                      </span>
                    </span>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy !== null}
                      onClick={() => setRestoreTarget(b.filename)}
                    >
                      Restore
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </main>

      <AlertDialog
        open={restoreTarget !== null}
        onOpenChange={(open) => {
          if (!open) {
            setRestoreTarget(null);
            setTotp("");
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Restore {restoreTarget}?</AlertDialogTitle>
            <AlertDialogDescription>
              This rewinds orders, trading state and risk limits to the backup. A safety snapshot of the current
              database is saved first, and live trading is disarmed afterwards.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {needCode && (
            <input
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={totp}
              onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
              placeholder="6-digit authenticator code"
              className="w-full rounded border px-3 py-2 text-sm"
            />
          )}
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button variant="destructive" disabled={busy !== null || !codeOk} onClick={restore}>
              {busy === "restore" ? "Restoring…" : "Restore"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
