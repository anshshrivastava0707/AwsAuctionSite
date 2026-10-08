"use client";

import { useEffect, useState } from "react";

/** The browser clock, ticking every `intervalMs`. null until mounted, so server and
 *  client markup agree (no hydration mismatch). */
export function useNow(intervalMs = 1000) {
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}
