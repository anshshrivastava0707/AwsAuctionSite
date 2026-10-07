"use client";

import { useState } from "react";
import { imageUrl, uploadImage } from "@/lib/api";

const MAX_IMAGES = 8; // backend models.MAX_IMAGES
const MAX_BYTES = 5 * 1024 * 1024; // backend storage.MAX_IMAGE_BYTES
const TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif"];

/** Uploads straight to storage as files are picked; `value` is the list of stored keys (first = cover). */
export default function ImagePicker({
  token,
  value,
  onChange,
  max = MAX_IMAGES,
}: {
  token: string;
  value: string[];
  onChange: (keys: string[]) => void;
  max?: number;
}) {
  const [busy, setBusy] = useState(0);
  const [error, setError] = useState<string | null>(null);

  async function onPick(files: FileList | null) {
    if (!files) return;
    setError(null);
    const picked = [...files].slice(0, max - value.length);
    const bad = picked.find((f) => !TYPES.includes(f.type) || f.size > MAX_BYTES);
    if (bad) {
      setError(`${bad.name}: use JPEG, PNG, WebP or GIF up to 5 MB.`);
      return;
    }
    setBusy((n) => n + picked.length);
    let keys = value;
    for (const file of picked) {
      try {
        keys = [...keys, await uploadImage(token, file)];
        onChange(keys);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Upload failed");
      } finally {
        setBusy((n) => n - 1);
      }
    }
  }

  const move = (i: number) => onChange([value[i], ...value.filter((_, j) => j !== i)]);
  const remove = (i: number) => onChange(value.filter((_, j) => j !== i));

  return (
    <div>
      <div className="picker">
        {value.map((key, i) => (
          <div className="tile" key={key}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={imageUrl(key)} alt="" />
            {i === 0 && max > 1 && <span className="cover">Cover</span>}
            <div className="tools">
              {i > 0 && <button type="button" onClick={() => move(i)}>Make cover</button>}
              <button type="button" onClick={() => remove(i)}>Remove</button>
            </div>
          </div>
        ))}
        {value.length + busy < max && (
          <label className="add">
            {busy > 0 ? "Uploading…" : "+ Add photo"}
            <input type="file" accept={TYPES.join(",")} multiple={max > 1}
                   onChange={(e) => { void onPick(e.target.files); e.target.value = ""; }} />
          </label>
        )}
        {busy > 0 && value.length + busy >= max && <div className="add">Uploading…</div>}
      </div>
      {error && <p className="notice bad" style={{ marginTop: 8 }}>{error}</p>}
    </div>
  );
}
