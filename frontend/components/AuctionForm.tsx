"use client";

import { useState } from "react";
import { centsToDollarString, formatDateTime, fromLocalInput, parseDollarsToCents, toLocalInput } from "@/lib/format";
import { CATEGORIES, CONDITIONS, type Auction, type Category, type Condition, type ListingInput } from "@/lib/types";
import ImagePicker from "./ImagePicker";

const DURATIONS: [string, number][] = [
  ["5 min", 5 * 60_000], ["1 hour", 3_600_000], ["1 day", 86_400_000], ["3 days", 3 * 86_400_000], ["7 days", 7 * 86_400_000],
];
const MIN_RUN_MS = 30_000;
const MAX_RUN_MS = 7 * 86_400_000;

/** Create (no `initial`) or edit an auction. On edit, only changed fields are submitted. */
export default function AuctionForm({
  token,
  initial,
  submitLabel,
  onSubmit,
}: {
  token: string;
  initial?: Auction;
  submitLabel: string;
  onSubmit: (input: Partial<ListingInput>) => Promise<void>;
}) {
  const [now] = useState(() => Date.now());
  const alreadyStarted = initial != null && initial.startsAt <= now;
  const [title, setTitle] = useState(initial?.title ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [category, setCategory] = useState<Category>(initial?.category ?? "OTHER");
  const [condition, setCondition] = useState<Condition>(initial?.condition ?? "GOOD");
  const [quantity, setQuantity] = useState(String(initial?.quantity ?? 1));
  const [images, setImages] = useState<string[]>(initial?.images ?? []);
  const [start, setStart] = useState(centsToDollarString(initial?.startingPrice ?? 1000));
  const [increment, setIncrement] = useState(centsToDollarString(initial?.minIncrement ?? 100));
  const [startNow, setStartNow] = useState(initial ? alreadyStarted : true);
  const [startsAt, setStartsAt] = useState(toLocalInput(initial?.startsAt ?? now + 3_600_000));
  const [endsAt, setEndsAt] = useState(toLocalInput(initial?.endsAt ?? now + 86_400_000));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function setDuration(ms: number) {
    const base = startNow ? Date.now() : fromLocalInput(startsAt) ?? Date.now();
    setEndsAt(toLocalInput(base + ms));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const startingPrice = parseDollarsToCents(start);
    const minIncrement = parseDollarsToCents(increment);
    const qty = Number(quantity);
    const clock = Date.now();
    // The inputs only hold minutes; an untouched field keeps the exact original time.
    const unchanged = (value: string, ms?: number) => ms != null && value === toLocalInput(ms);
    const startMs = startNow
      ? (alreadyStarted ? initial!.startsAt : clock)
      : unchanged(startsAt, initial?.startsAt) ? initial!.startsAt : fromLocalInput(startsAt);
    const endMs = unchanged(endsAt, initial?.endsAt) ? initial!.endsAt : fromLocalInput(endsAt);

    if (!title.trim()) return setError("Enter a title.");
    if (startingPrice == null) return setError("Enter a valid starting price.");
    if (minIncrement == null || minIncrement < 1) return setError("The increment must be at least $0.01.");
    if (!Number.isInteger(qty) || qty < 1 || qty > 1000) return setError("Quantity must be a whole number from 1 to 1000.");
    if (startMs == null || endMs == null) return setError("Pick valid start and end times.");
    if (!startNow && startMs < clock - 60_000 && startMs !== initial?.startsAt) return setError("The start time is in the past.");
    if (endMs - Math.max(startMs, clock) < MIN_RUN_MS) return setError("The auction must run for at least 30 seconds.");
    if (endMs - startMs > MAX_RUN_MS) return setError("An auction can run for at most 7 days.");

    const full: ListingInput = {
      title: title.trim(), description: description.trim(), category, condition, quantity: qty, images,
      startingPrice, minIncrement, startsAt: startMs, endsAt: endMs,
    };
    let payload: Partial<ListingInput> = full;
    if (initial) {
      payload = Object.fromEntries(
        Object.entries(full).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(initial[k as keyof Auction])),
      );
      if (Object.keys(payload).length === 0) return setError("Nothing has changed.");
    }
    setBusy(true);
    setError(null);
    try {
      await onSubmit(payload);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={submit}>
      <label>
        Title
        <input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={120} />
      </label>
      <label>
        Description
        <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={4} maxLength={2000} />
      </label>

      <div>
        <div className="small muted" style={{ marginBottom: 4 }}>Photos (up to 8, the first is the cover)</div>
        <ImagePicker token={token} value={images} onChange={setImages} />
      </div>

      <div className="row">
        <label>
          Category
          <select value={category} onChange={(e) => setCategory(e.target.value as Category)}>
            {Object.entries(CATEGORIES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label>
          Condition
          <select value={condition} onChange={(e) => setCondition(e.target.value as Condition)}>
            {Object.entries(CONDITIONS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label>
          Quantity
          <input value={quantity} onChange={(e) => setQuantity(e.target.value)} inputMode="numeric" />
        </label>
      </div>
      <p className="hint" style={{ margin: 0 }}>
        Quantity above 1 means the items are sold together as one lot to the single winner.
      </p>

      <div className="row">
        <label>
          Starting price ($)
          <input value={start} onChange={(e) => setStart(e.target.value)} inputMode="decimal" required />
        </label>
        <label>
          Min increment ($)
          <input value={increment} onChange={(e) => setIncrement(e.target.value)} inputMode="decimal" required />
        </label>
      </div>

      {alreadyStarted ? (
        <p className="small muted" style={{ margin: 0 }}>Started {formatDateTime(initial!.startsAt)}</p>
      ) : (
        <>
          <label className="check">
            <input type="checkbox" checked={startNow} onChange={(e) => setStartNow(e.target.checked)} />
            Start immediately
          </label>
          {!startNow && (
            <label>
              Starts at
              <input type="datetime-local" value={startsAt} onChange={(e) => setStartsAt(e.target.value)} />
            </label>
          )}
        </>
      )}
      <label>
        Ends at
        <input type="datetime-local" value={endsAt} onChange={(e) => setEndsAt(e.target.value)} />
      </label>
      <div className="actions">
        <span className="hint">Run for:</span>
        {DURATIONS.map(([label, ms]) => (
          <button key={label} type="button" className="secondary small" onClick={() => setDuration(ms)}>{label}</button>
        ))}
      </div>

      {error && <p className="notice bad">{error}</p>}
      <button type="submit" disabled={busy}>{busy ? "Saving…" : submitLabel}</button>
    </form>
  );
}
