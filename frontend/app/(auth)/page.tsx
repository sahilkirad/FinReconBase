"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { FanOutVisual } from "@/components/onboarding/fan-out-visual";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, extractApiError } from "@/lib/api";
import { useAuthStore, type VendorProfile } from "@/store/auth";

type Mode = "login" | "register";

interface AuthResponse {
  access_token: string;
  vendor_code: string;
  vendor_name: string;
  role: string;
}

export default function OnboardingPage() {
  const router = useRouter();
  const { token, signIn } = useAuthStore();
  const [mode, setMode] = useState<Mode>("login");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Form fields
  const [vendorCode, setVendorCode] = useState("");
  const [vendorName, setVendorName] = useState("");
  const [email, setEmail] = useState("");
  const [secret, setSecret] = useState("");

  // Already authenticated (persisted session) -> straight into the app.
  useEffect(() => {
    if (token) {
      router.replace("/reconciliation");
    }
  }, [token, router]);

  function switchMode(next: Mode) {
    setMode(next);
    setError(null);
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const url = mode === "login" ? "/auth/vendor/token" : "/auth/vendor/register";
      const body =
        mode === "login"
          ? { vendor_code: vendorCode, api_secret: secret }
          : {
              vendor_code: vendorCode,
              vendor_name: vendorName,
              email,
              api_secret: secret,
            };
      const { data } = await api.post<AuthResponse>(url, body);
      const profile: VendorProfile = {
        vendor_code: data.vendor_code,
        vendor_name: data.vendor_name,
        role: data.role,
      };
      signIn(data.access_token, profile);
      router.replace("/reconciliation");
    } catch (err) {
      setError(extractApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const isLogin = mode === "login";

  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-navy px-4 py-10">
      {/* Background animation layer — never stacks above the auth card */}
      <div className="absolute inset-0 z-0 overflow-hidden" aria-hidden>
        <FanOutVisual />
      </div>

      <div className="relative z-10 grid w-full max-w-5xl items-center gap-10 lg:grid-cols-2">
        {/* Left: value proposition */}
        <section className="hidden flex-col items-center text-center lg:flex">
          <p className="mb-2 text-xs font-semibold uppercase tracking-[0.25em] text-primary">
            Enterprise Finance Operations
          </p>
          <h1 className="mt-6 max-w-md text-3xl font-semibold leading-tight text-white">
            One invoice batch in.
            <br />
            A perfectly balanced ledger out.
          </h1>
          <p className="mt-3 max-w-md text-sm leading-relaxed text-slate-300">
            Autonomous financial reconciliation. Ingest unstructured invoices,
            automatically synchronize settlement feeds, and post perfectly
            balanced entries to an immutable ledger.
          </p>
        </section>

        {/* Right: native vendor auth card */}
        <section className="relative z-10 rounded-xl border border-white/10 bg-white p-8 shadow-xl">
          <div className="mb-6">
            <p className="font-mono text-xs font-semibold uppercase tracking-widest text-primary">
              Vellum
            </p>
            <h2 className="mt-1 text-xl font-semibold text-navy">
              {isLogin ? "Vendor sign in" : "Provision a vendor tenant"}
            </h2>
            <p className="mt-1 text-sm text-slate-500">
              {isLogin
                ? "Use the vendor code and API secret issued to your tenant."
                : "Register a new tenant — you are signed in immediately."}
            </p>
          </div>

          {/* Mode tabs */}
          <div className="mb-6 grid grid-cols-2 rounded-lg bg-slate-100 p-1 text-sm font-medium">
            {(["login", "register"] as Mode[]).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => switchMode(m)}
                className={
                  mode === m
                    ? "rounded-md bg-white py-1.5 text-navy shadow-sm"
                    : "rounded-md py-1.5 text-slate-500 hover:text-navy"
                }
              >
                {m === "login" ? "Sign in" : "Register"}
              </button>
            ))}
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            {!isLogin && (
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-600">
                  Vendor name
                </span>
                <Input
                  required
                  value={vendorName}
                  onChange={(e) => setVendorName(e.target.value)}
                  placeholder="Nexus Logistics Pvt Ltd"
                  minLength={2}
                />
              </label>
            )}
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-slate-600">
                Vendor code
              </span>
              <Input
                required
                value={vendorCode}
                onChange={(e) => setVendorCode(e.target.value)}
                placeholder="VEND_NEXUS_001"
                className="font-mono uppercase"
                minLength={3}
                maxLength={64}
              />
            </label>
            {!isLogin && (
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-600">
                  Email
                </span>
                <Input
                  required
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="finance@nexus.example"
                />
              </label>
            )}
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-slate-600">
                API secret
              </span>
              <Input
                required
                type="password"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                placeholder={isLogin ? "Your tenant API secret" : "At least 8 characters"}
                minLength={isLogin ? 1 : 8}
              />
            </label>

            {error && (
              <p className="rounded-md bg-danger-soft px-3 py-2 text-xs text-danger">
                {error}
              </p>
            )}

            <Button type="submit" disabled={busy} className="w-full">
              {busy
                ? "Authenticating…"
                : isLogin
                  ? "Sign in →"
                  : "Create tenant & sign in →"}
            </Button>
          </form>

          <p className="mt-4 text-center font-mono text-[11px] text-slate-400">
            Secure B2B Tenant Access · End-to-End Encryption
          </p>
        </section>
      </div>
    </main>
  );
}
