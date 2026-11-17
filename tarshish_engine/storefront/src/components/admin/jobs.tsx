"use client";

// Background jobs in the browser: start one, and its console output streams into the drawer at
// the bottom of every admin page (polling GET /api/admin/jobs/{id}?since=<lines already shown>).

import { ChevronDown, ChevronUp, Copy, SquareTerminal, Trash2 } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { adminApi, errorText, timeAgo } from "@/lib/admin/client";
import type { Job } from "@/lib/admin/types";

import { StatusBadge, useToast } from "./ui";

type Tracked = Job & { lines: string[] };

type JobsApi = {
  jobs: Tracked[];
  /** POST an action that answers with a job, then follow it. Resolves with the finished job. */
  start: (path: string, body?: unknown, label?: string) => Promise<Job | null>;
  /** Show an existing job (e.g. from the history list) in the drawer. */
  follow: (job: Job) => void;
  open: boolean;
  setOpen: (open: boolean) => void;
  /** Bumped whenever a job finishes, so pages can reload their data. */
  finishedCount: number;
};

const JobsContext = createContext<JobsApi | null>(null);

export function useJobs(): JobsApi {
  const value = useContext(JobsContext);
  if (!value) throw new Error("useJobs() outside <JobsProvider>");
  return value;
}

const POLL_MS = 1000;

export function JobsProvider({ children }: { children: ReactNode }) {
  const toast = useToast();
  const [jobs, setJobs] = useState<Tracked[]>([]);
  const [open, setOpen] = useState(false);
  const [finishedCount, setFinishedCount] = useState(0);
  const waiters = useRef(new Map<string, (job: Job) => void>());
  const inFlight = useRef(new Set<string>());
  const jobsRef = useRef(jobs);
  jobsRef.current = jobs;

  const upsert = useCallback((job: Job, replaceLines = false) => {
    setJobs((list) => {
      const existing = list.find((item) => item.id === job.id);
      const lines = existing && !replaceLines ? [...existing.lines, ...job.log] : job.log;
      const next: Tracked = { ...job, lines };
      return existing ? list.map((item) => (item.id === job.id ? next : item)) : [next, ...list].slice(0, 12);
    });
  }, []);

  const follow = useCallback(
    (job: Job) => {
      setOpen(true);
      void adminApi<Job>(`jobs/${job.id}`).then((full) => upsert(full, true), () => upsert(job, true));
    },
    [upsert],
  );

  const start = useCallback(
    async (path: string, body?: unknown, label?: string) => {
      try {
        const job = await adminApi<Job>(path, { method: "POST", body: body ?? {} });
        upsert(job, true);
        setOpen(true);
        toast("ok", `${label ?? job.kind} started`);
        return await new Promise<Job>((resolve) => waiters.current.set(job.id, resolve));
      } catch (error) {
        toast("error", errorText(error));
        return null;
      }
    },
    [toast, upsert],
  );

  // Poll every unfinished job, asking only for the lines not shown yet.
  useEffect(() => {
    const timer = window.setInterval(() => {
      for (const job of jobsRef.current) {
        if (job.done || inFlight.current.has(job.id)) continue;
        inFlight.current.add(job.id);
        void adminApi<Job>(`jobs/${job.id}?since=${job.lines.length}`)
          .finally(() => inFlight.current.delete(job.id))
          .then((fresh) => {
            upsert(fresh);
            if (fresh.done) {
              setFinishedCount((n) => n + 1);
              toast(fresh.status === "SUCCEEDED" ? "ok" : "error", `${fresh.kind}: ${fresh.status.toLowerCase()}`);
              waiters.current.get(fresh.id)?.(fresh);
              waiters.current.delete(fresh.id);
            }
          })
          .catch(() => undefined);
      }
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [toast, upsert]);

  const value = useMemo(
    () => ({ jobs, start, follow, open, setOpen, finishedCount }),
    [jobs, start, follow, open, finishedCount],
  );
  return <JobsContext.Provider value={value}>{children}</JobsContext.Provider>;
}

// --------------------------------------------------------------------------- //
// Console output drawer
// --------------------------------------------------------------------------- //
function lineClass(line: string): string {
  if (/^\s*(✗|\[ERROR\]|\[CONFIG ERROR\]|\[BUDGET HALT\]|\[FAILED\])|\bERROR\b/.test(line)) return "text-red-400";
  if (/^\s*(!|.*\bWARNING\b)/.test(line)) return "text-amber-300";
  if (/^\s*(✓|\[SUCCEEDED\])/.test(line)) return "text-emerald-400";
  if (line.startsWith("$ ")) return "text-sky-300";
  return "text-zinc-200";
}

export function ConsoleDrawer() {
  const { jobs, open, setOpen } = useJobs();
  const toast = useToast();
  const [selected, setSelected] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const scroller = useRef<HTMLDivElement>(null);
  const visible = jobs.filter((job) => !hidden.has(job.id));
  const active = visible.find((job) => job.id === selected) ?? visible[0];
  const running = jobs.filter((job) => !job.done).length;

  useEffect(() => {
    if (jobs[0] && !hidden.has(jobs[0].id) && !jobs[0].done) setSelected(jobs[0].id);
  }, [jobs, hidden]);

  useEffect(() => {
    const box = scroller.current;
    if (box && box.scrollHeight - box.scrollTop - box.clientHeight < 120) box.scrollTop = box.scrollHeight;
  }, [active?.lines.length, open]);

  return (
    <div className="fixed inset-x-0 bottom-0 z-40 lg:left-60">
      <div className="mx-auto border-t border-adm-line bg-adm-panel shadow-[0_-8px_30px_rgba(0,0,0,0.18)]">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm font-medium hover:bg-adm-raised"
          aria-expanded={open}
        >
          <SquareTerminal className="h-4 w-4 text-adm-accent" />
          Console output
          {running > 0 && <StatusBadge status="RUNNING" />}
          {active && !open && <span className="truncate text-xs text-adm-faint">· {active.command}</span>}
          <span className="ml-auto text-adm-soft">{open ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}</span>
        </button>
        {open && (
          <div className="border-t border-adm-line">
            {visible.length === 0 ? (
              <p className="px-4 py-6 text-sm text-adm-soft">
                No jobs yet. Start a harvest, a publishing run or a connection check and its output streams here.
              </p>
            ) : (
              <>
                <div className="flex gap-1 overflow-x-auto px-3 pt-2">
                  {visible.map((job) => (
                    <button
                      key={job.id}
                      type="button"
                      onClick={() => setSelected(job.id)}
                      className={`flex shrink-0 items-center gap-1.5 rounded-t-md border border-b-0 px-2.5 py-1 text-xs ${
                        job.id === active?.id ? "border-adm-line bg-adm-console text-zinc-100" : "border-transparent text-adm-soft hover:bg-adm-raised"
                      }`}
                    >
                      <span
                        className={`h-1.5 w-1.5 rounded-full ${
                          !job.done ? "animate-pulse bg-sky-400" : job.status === "SUCCEEDED" ? "bg-emerald-400" : "bg-red-400"
                        }`}
                      />
                      {job.kind}
                      <span className="opacity-60">{timeAgo(job.created_at)}</span>
                    </button>
                  ))}
                </div>
                {active && (
                  <div className="bg-adm-console">
                    <div className="flex flex-wrap items-center gap-2 border-b border-white/10 px-4 py-1.5 text-xs text-zinc-400">
                      <span className="adm-mono truncate">{active.command}</span>
                      <StatusBadge status={active.status} />
                      {active.exit_code !== null && <span>exit {active.exit_code}</span>}
                      <span className="ml-auto flex gap-1">
                        <button
                          type="button"
                          className="rounded p-1 hover:bg-white/10"
                          title="Copy output"
                          onClick={() => {
                            void navigator.clipboard?.writeText(active.lines.join("\n")).then(() => toast("ok", "Output copied"));
                          }}
                        >
                          <Copy className="h-3.5 w-3.5" />
                        </button>
                        <button
                          type="button"
                          className="rounded p-1 hover:bg-white/10"
                          title="Close this tab"
                          onClick={() => setHidden((set) => new Set(set).add(active.id))}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </span>
                    </div>
                    <div ref={scroller} className="adm-mono h-[38vh] overflow-auto px-4 py-2 text-[12px] leading-[1.55]">
                      {active.lines.map((line, index) => (
                        <div key={index} className={`whitespace-pre-wrap break-words ${lineClass(line)}`}>
                          {line || " "}
                        </div>
                      ))}
                      {!active.done && <div className="animate-pulse text-zinc-500">▍</div>}
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
