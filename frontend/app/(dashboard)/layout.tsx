"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { ErrorBoundary } from "@/components/error-boundary";
import {
  batchIdFromPath,
  isLiveBatchPath,
  readActiveBatch,
  rememberActiveBatch,
} from "@/lib/active-batch";
import { api } from "@/lib/api";
import { useAuthStore } from "@/store/auth";

const NAV = [
  { href: "/reconciliation", label: "Command Center", live: false },
  { href: "/ledger", label: "Immutable Ledger", live: false },
  { href: "/exceptions", label: "Exception Desk", live: false },
];

function navActive(href: string, pathname: string, live: boolean): boolean {
  if (live) {
    // The live batch route (exact URL prefix).
    return pathname.startsWith(href);
  }
  if (href === "/reconciliation") {
    // Command Center is the wizard only — never highlight it on the live page.
    return pathname === "/reconciliation";
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const { token, profile, signOut } = useAuthStore();
  const vendorCode = profile?.vendor_code ?? null;
  // SSR/hydration: the guard must not flash the app before sessionStorage loads.
  const [mounted, setMounted] = useState(false);
  const [liveBatch, setLiveBatch] = useState<string | null>(null);

  useEffect(() => setMounted(true), []);

  // Derive the Financial Operations Center batch from the URL when present,
  // otherwise fall back to the last-watched batch (sessionStorage, scoped to
  // this vendor so a stale pointer from another tenant is never used).
  useEffect(() => {
    if (isLiveBatchPath(pathname)) {
      const fromPath = batchIdFromPath(pathname);
      if (fromPath && vendorCode) rememberActiveBatch(fromPath, vendorCode);
      setLiveBatch(fromPath);
    } else {
      setLiveBatch(vendorCode ? readActiveBatch(vendorCode) : null);
    }
  }, [pathname, vendorCode]);

  // DB-backed rehydration: after a fresh sign-in the sessionStorage pointer is
  // gone (it is per-tab), so ask the backend for this vendor's most recent
  // batch. Without this the Financial Operations nav item would stay hidden
  // until a brand-new batch is run.
  useEffect(() => {
    if (!mounted || !token || !vendorCode || liveBatch) return;
    let cancelled = false;
    api
      .get<{ batch_id: string }>("/batches/latest")
      .then(({ data }) => {
        if (!cancelled && data?.batch_id) {
          setLiveBatch(data.batch_id);
          rememberActiveBatch(data.batch_id, vendorCode);
        }
      })
      .catch(() => {
        /* 404 = vendor has no batches yet — keep the item hidden */
      });
    return () => {
      cancelled = true;
    };
  }, [mounted, token, vendorCode, liveBatch]);

  useEffect(() => {
    if (mounted && !token) {
      router.replace("/");
    }
  }, [mounted, token, router]);

  if (!mounted || !token || !profile) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-line border-t-primary" />
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col bg-canvas">
      {/* Prussian Blue header */}
      <header className="sticky top-0 z-20 border-b border-white/10 bg-navy text-white">
        <div className="mx-auto flex h-14 max-w-7xl items-center justify-between px-4 sm:px-6">
          <div className="flex items-center gap-8">
            <Link href="/reconciliation" className="flex items-baseline gap-2">
              <span className="font-mono text-sm font-semibold tracking-widest text-primary">
                Vellum
              </span>
              <span className="hidden text-[11px] uppercase tracking-wider text-slate-400 sm:inline">
                AI Finance Controller
              </span>
            </Link>

            <nav className="hidden items-center gap-1 md:flex">
              {NAV.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className={clsx(
                    "rounded-md px-3 py-1.5 text-sm transition-colors",
                    navActive(item.href, pathname, false)
                      ? "bg-white/10 text-white"
                      : "text-slate-300 hover:bg-white/5 hover:text-white"
                  )}
                >
                  {item.label}
                </Link>
              ))}
              {liveBatch && (
                <Link
                  href={`/reconciliation/${liveBatch}`}
                  className={clsx(
                    "ml-1 flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm transition-colors",
                    navActive(`/reconciliation/${liveBatch}`, pathname, true)
                      ? "border-primary/50 bg-primary/15 text-white"
                      : "border-white/15 text-slate-200 hover:border-primary/40 hover:text-white"
                  )}
                >
                  <span
                    className={clsx(
                      "h-1.5 w-1.5 rounded-full",
                      navActive(`/reconciliation/${liveBatch}`, pathname, true)
                        ? "bg-primary"
                        : "animate-pulse bg-primary/70"
                    )}
                  />
                  Financial Operations
                </Link>
              )}
            </nav>
          </div>

          {/* JWT-scope proof: profile rendered from the persisted session */}
          <div className="flex items-center gap-3">
            <div className="hidden text-right sm:block">
              <p className="text-sm font-medium leading-tight">
                {profile.vendor_name}
              </p>
              <p className="font-mono text-[11px] leading-tight text-slate-400">
                {profile.vendor_code}
              </p>
            </div>
            <span
              className={clsx(
                "rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider",
                profile.role === "ADMIN"
                  ? "bg-primary/20 text-primary"
                  : "bg-white/10 text-slate-300"
              )}
            >
              {profile.role}
            </span>
            <button
              type="button"
              onClick={() => {
                signOut();
                router.replace("/");
              }}
              className="rounded-md border border-white/15 px-2.5 py-1 text-xs text-slate-300 transition-colors hover:border-primary/50 hover:text-white"
            >
              Sign out
            </button>
          </div>
        </div>

        {/* Mobile nav */}
        <nav className="flex gap-1 overflow-x-auto px-4 pb-2 md:hidden">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={clsx(
                "whitespace-nowrap rounded-md px-3 py-1 text-sm",
                navActive(item.href, pathname, false)
                  ? "bg-white/10 text-white"
                  : "text-slate-300"
              )}
            >
              {item.label}
            </Link>
          ))}
          {liveBatch && (
            <Link
              href={`/reconciliation/${liveBatch}`}
              className={clsx(
                "whitespace-nowrap rounded-md px-3 py-1 text-sm",
                navActive(`/reconciliation/${liveBatch}`, pathname, true)
                  ? "bg-white/10 text-white"
                  : "text-slate-300"
              )}
            >
              ◉ Financial Operations
            </Link>
          )}
        </nav>
      </header>

      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6">
        <ErrorBoundary label="page">{children}</ErrorBoundary>
      </main>

      <footer className="border-t border-line py-3 text-center text-[11px] text-slate-400">
        Every cleared transaction is locked into the enterprise General Ledger
      </footer>
    </div>
  );
}
