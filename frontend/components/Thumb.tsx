"use client";

import { useState } from "react";
import { imageUrl } from "@/lib/api";

/** The small WebP thumbnail, falling back to the original if it isn't made yet
 *  (it's generated a moment after upload) or never will be (older images). */
export default function Thumb({ imageKey }: { imageKey: string }) {
  const [full, setFull] = useState(false);
  return (
    // eslint-disable-next-line @next/next/no-img-element -- images are served by our API
    <img
      className="thumb"
      src={imageUrl(imageKey, full ? undefined : "thumb")}
      alt=""
      loading="lazy"
      onError={() => setFull(true)}
    />
  );
}
