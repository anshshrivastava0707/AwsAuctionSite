"use client";

import { useState } from "react";
import { imageUrl } from "@/lib/api";

export default function Gallery({ images, title }: { images: string[]; title: string }) {
  const [index, setIndex] = useState(0);
  if (images.length === 0) return null;
  const current = images[Math.min(index, images.length - 1)];
  return (
    <div className="gallery">
      {/* eslint-disable-next-line @next/next/no-img-element -- images are served by our API */}
      <img className="main" src={imageUrl(current)} alt={title} />
      {images.length > 1 && (
        <div className="strip">
          {images.map((key, i) => (
            <button key={key} type="button" className={key === current ? "active" : undefined}
                    onClick={() => setIndex(i)} aria-label={`Photo ${i + 1}`}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={imageUrl(key)} alt="" />
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
