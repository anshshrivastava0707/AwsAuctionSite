"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth";
import { cognitoEnabled, confirmForgotPassword, confirmSignUp, CognitoError, forgotPassword, resendCode, signUp } from "@/lib/cognito";

type Step = "signin" | "signup" | "confirm" | "forgot" | "reset";

interface DemoAccount { label: string; email: string; password: string; note?: string }

/** Ready-made accounts for judges/reviewers (backend/scripts/create_demo_accounts.py).
 *  Only shown where NEXT_PUBLIC_DEMO_ACCOUNTS is set, i.e. on demo deployments. */
const DEMO_ACCOUNTS: DemoAccount[] = (() => {
  try {
    const list = JSON.parse(process.env.NEXT_PUBLIC_DEMO_ACCOUNTS ?? "[]");
    return Array.isArray(list) ? list.filter((a) => a && a.email && a.password) : [];
  } catch {
    return [];
  }
})();

const TITLES: Record<Step, string> = {
  signin: "Log in",
  signup: "Create your account",
  confirm: "Check your email",
  forgot: "Reset your password",
  reset: "Choose a new password",
};

export default function LoginForm({ next, signup }: { next: string; signup: boolean }) {
  const { status, login } = useAuth();
  const router = useRouter();
  const [step, setStep] = useState<Step>(signup && cognitoEnabled ? "signup" : "signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  useEffect(() => {
    if (status === "ready") router.replace(next);
    // New accounts land on /account (to see their approval status) unless they were headed somewhere.
    if (status === "onboarding") router.replace(next === "/" ? "/onboarding" : `/onboarding?next=${encodeURIComponent(next)}`);
  }, [status, next, router]);

  const go = (s: Step, message: string | null = null) => {
    setStep(s);
    setError(null);
    setInfo(message);
    setCode("");
  };

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      if (err instanceof CognitoError && err.code === "UserNotConfirmedException") {
        await resendCode(email).catch(() => undefined);
        go("confirm", "Your email isn't confirmed yet. We've sent you a new code.");
      } else {
        setError(err instanceof Error ? err.message : "Something went wrong");
      }
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    void run(async () => {
      switch (step) {
        case "signin":
          await login(email, password);
          break;
        case "signup": {
          const r = await signUp(email.trim(), password);
          if (r.UserConfirmed) await login(email, password);
          else go("confirm", `We sent a 6-digit code to ${email.trim()}.`);
          break;
        }
        case "confirm":
          await confirmSignUp(email.trim(), code);
          if (password) await login(email, password);
          else go("signin", "Email confirmed. You can log in now.");
          break;
        case "forgot":
          await forgotPassword(email.trim());
          go("reset", `If ${email.trim()} has an account, we've emailed it a code.`);
          break;
        case "reset":
          await confirmForgotPassword(email.trim(), code, password);
          await login(email, password);
          break;
      }
    });
  }

  if (!cognitoEnabled) {
    return (
      <div className="card narrow">
        <h1>{signup ? "Create an account" : "Log in"}</h1>
        <p className="notice warn">
          Development login: there are no passwords on this server, so anyone can sign in as any email.
        </p>
        <form className="stack" onSubmit={onSubmit}>
          <label>
            Email
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
          </label>
          {error && <p className="notice bad">{error}</p>}
          <button type="submit" disabled={busy}>{busy ? "Signing in…" : signup ? "Continue" : "Log in"}</button>
        </form>
        <p className="small muted">New here? Signing in with a new email starts account setup.</p>
      </div>
    );
  }

  const needsPassword = step === "signin" || step === "signup" || step === "reset";
  const needsCode = step === "confirm" || step === "reset";
  function logInAsDemo(a: DemoAccount) {
    setEmail(a.email);
    setPassword(a.password);
    void run(() => login(a.email, a.password));
  }

  const submitLabel = {
    signin: "Log in", signup: "Create account", confirm: "Confirm email", forgot: "Send reset code", reset: "Set password",
  }[step];

  return (
    <div className="card narrow auth-card">
      <h1>{TITLES[step]}</h1>
      {info && <p className="notice ok">{info}</p>}
      <form className="stack" onSubmit={onSubmit}>
        <label>
          Email
          <input
            type="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoFocus={!needsCode}
            readOnly={needsCode}
          />
        </label>
        {needsCode && (
          <label>
            Code from the email
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              inputMode="numeric"
              autoComplete="one-time-code"
              required
              autoFocus
            />
          </label>
        )}
        {needsPassword && (
          <label>
            {step === "reset" ? "New password" : "Password"}
            <input
              type="password"
              autoComplete={step === "signin" ? "current-password" : "new-password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              minLength={step === "signin" ? undefined : 8}
              required
            />
            {step !== "signin" && <span className="hint">At least 8 characters, with a lowercase letter and a number.</span>}
          </label>
        )}
        {error && <p className="notice bad">{error}</p>}
        <button type="submit" disabled={busy}>{busy ? "Please wait…" : submitLabel}</button>
      </form>

      {step === "signin" && DEMO_ACCOUNTS.length > 0 && (
        <div className="demo-accounts">
          <div className="demo-head">
            <strong>Judging or just looking?</strong>
            <span className="hint">Log in with a ready-made account. Use two browsers to bid against each other.</span>
          </div>
          {DEMO_ACCOUNTS.map((a) => (
            <div key={a.email} className="demo-row">
              <div>
                <div className="demo-label">{a.label}</div>
                {a.note && <div className="hint">{a.note}</div>}
                <div className="demo-cred"><code>{a.email}</code> · <code>{a.password}</code></div>
              </div>
              <button type="button" className="secondary small" disabled={busy} onClick={() => logInAsDemo(a)}>
                Log in
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="auth-links small">
        {step === "signin" && (
          <>
            <button type="button" className="link" onClick={() => go("forgot")}>Forgot password?</button>
            <span>New here? <button type="button" className="link" onClick={() => go("signup")}>Create an account</button></span>
          </>
        )}
        {step === "signup" && (
          <span>Already have an account? <button type="button" className="link" onClick={() => go("signin")}>Log in</button></span>
        )}
        {step === "confirm" && (
          <button type="button" className="link" disabled={busy}
            onClick={() => void run(async () => { await resendCode(email.trim()); setInfo("We sent a new code."); })}>
            Send a new code
          </button>
        )}
        {(step === "forgot" || step === "reset" || step === "confirm") && (
          <button type="button" className="link" onClick={() => go("signin")}>Back to log in</button>
        )}
      </div>
    </div>
  );
}
