import LoginForm from "./LoginForm";

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string; signup?: string }> }) {
  const { next, signup } = await searchParams;
  // Only allow same-site redirects after login.
  const safeNext = next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
  return <LoginForm next={safeNext} signup={signup === "1"} />;
}
