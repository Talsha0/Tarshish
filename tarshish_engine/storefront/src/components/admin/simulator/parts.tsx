"use client";

// Small pieces shared by the platform skins: the FindHub account avatar and the post media.

import { ImageOff } from "lucide-react";
import { useState } from "react";

import type { PreviewItem } from "./model";

export type Appearance = "light" | "dark";

/** The FindHub account avatar, with Instagram's gradient ring when `ring` is set. */
export function Avatar({ size = 32, ring = false, label = "F" }: { size?: number; ring?: boolean; label?: string }) {
  const inner = (
    <span
      className="flex items-center justify-center rounded-full bg-[#0f6e52] font-bold text-[#ffd23f] shadow-inner"
      style={{ width: size, height: size, fontSize: size * 0.45 }}
      aria-hidden
    >
      {label}
    </span>
  );
  if (!ring) return inner;
  return (
    <span className="rounded-full bg-gradient-to-tr from-[#feda75] via-[#d62976] to-[#4f5bd5] p-[2px]">
      <span className="block rounded-full bg-black p-[2px]">{inner}</span>
    </span>
  );
}

/** The post's visual: a muted looping video when there is one, else the branded image, else a placeholder. */
export function PreviewMedia({ item, className = "", fit = "cover" }: { item: PreviewItem; className?: string; fit?: "cover" | "contain" }) {
  const [failed, setFailed] = useState<string | null>(null);
  const src = item.imageUrl;
  if (item.videoUrl && failed !== item.videoUrl) {
    return (
      <video
        key={item.videoUrl}
        src={item.videoUrl}
        poster={src ?? undefined}
        className={`h-full w-full ${fit === "contain" ? "object-contain" : "object-cover"} ${className}`}
        autoPlay
        muted
        loop
        playsInline
        onError={() => setFailed(item.videoUrl)}
      />
    );
  }
  if (src && failed !== src) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        key={src}
        src={src}
        alt={item.productTitle}
        className={`h-full w-full ${fit === "contain" ? "object-contain" : "object-cover"} ${className}`}
        draggable={false}
        onError={() => setFailed(src)}
      />
    );
  }
  return (
    <div className={`flex h-full w-full flex-col items-center justify-center gap-2 bg-gradient-to-br from-[#0f6e52] to-[#0a3d2e] p-6 text-center text-white ${className}`}>
      <ImageOff className="h-7 w-7 opacity-70" />
      <p className="line-clamp-3 text-sm font-semibold" dir="auto">
        {item.productTitle}
      </p>
      <p className="text-[11px] opacity-70">FindHub · no image rendered yet</p>
    </div>
  );
}
