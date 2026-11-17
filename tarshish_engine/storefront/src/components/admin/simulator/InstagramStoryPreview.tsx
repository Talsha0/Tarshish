"use client";

// Instagram Story: full-bleed 9:16 image, progress bars, account header, the clickable link
// stickers stored on the asset (links_payload_json.story_links) and the "Send message" bar.

import { motion } from "framer-motion";
import { Ellipsis, Heart, Link2, Send, X } from "lucide-react";

import { chromeFor, type PreviewItem } from "./model";
import { Avatar, PreviewMedia } from "./parts";
import { SAFE_TOP } from "./screen";

function host(url: string): string {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}

export function InstagramStoryPreview({ item }: { item: PreviewItem }) {
  const t = chromeFor(item.language);
  const stickers = item.links.stickers;
  return (
    <div className="flex h-full flex-col bg-black text-white" style={{ paddingTop: SAFE_TOP - 6 }}>
      <div className="relative aspect-[9/16] w-full shrink-0 overflow-hidden rounded-xl bg-zinc-900">
        <PreviewMedia item={item} />
        <div className="pointer-events-none absolute inset-x-0 top-0 h-28 bg-gradient-to-b from-black/55 to-transparent" />

        {/* Progress bars: this Story plays first */}
        <div className="absolute inset-x-2 top-2 flex gap-1" dir="ltr">
          {[0, 1, 2].map((index) => (
            <span key={index} className="h-[2px] flex-1 overflow-hidden rounded-full bg-white/35">
              {index === 0 && (
                <motion.span
                  key={item.key}
                  className="block h-full bg-white"
                  initial={{ width: "0%" }}
                  animate={{ width: "100%" }}
                  transition={{ duration: 5, ease: "linear", repeat: Infinity, repeatDelay: 0.4 }}
                />
              )}
            </span>
          ))}
        </div>

        {/* Header */}
        <div className="absolute inset-x-3 top-5 flex items-center gap-2" dir={item.dir}>
          <Avatar size={32} />
          <p className="text-[13.5px] font-semibold drop-shadow">{item.handle.replace(/^@/, "")}</p>
          <p className="text-[13px] text-white/75 drop-shadow">2h</p>
          <span className="ms-auto flex items-center gap-3">
            <Ellipsis className="h-6 w-6 drop-shadow" />
            <X className="h-7 w-7 drop-shadow" strokeWidth={1.75} />
          </span>
        </div>

        {/* Link stickers */}
        {stickers.length > 0 && (
          <div className="absolute inset-x-0 bottom-[6%] flex flex-col items-center gap-2.5 px-6">
            {stickers.map((sticker, index) => (
              <motion.a
                key={`${sticker.kind}:${sticker.url}`}
                href={sticker.url}
                target="_blank"
                rel="noreferrer noopener"
                title={`${sticker.label} → ${host(sticker.url)}`}
                initial={{ opacity: 0, scale: 0.85, y: 8 }}
                animate={{ opacity: 1, scale: 1, y: 0, rotate: index % 2 ? 1.5 : -1.5 }}
                transition={{ delay: 0.12 * index, type: "spring", stiffness: 320, damping: 20 }}
                whileHover={{ scale: 1.05, rotate: 0 }}
                className="flex max-w-full items-center gap-1.5 rounded-lg bg-white px-3 py-2 shadow-[0_6px_18px_rgba(0,0,0,0.35)]"
              >
                <Link2 className="h-4 w-4 shrink-0 -rotate-45 text-[#2f6cf6]" strokeWidth={2.5} />
                <span className="truncate bg-gradient-to-r from-[#2f6cf6] to-[#8a3ab9] bg-clip-text text-[13px] font-bold uppercase tracking-wide text-transparent">
                  {sticker.label}
                </span>
              </motion.a>
            ))}
          </div>
        )}
      </div>

      {/* Reply bar */}
      <div className="flex flex-1 items-center gap-3 px-3" dir={item.dir}>
        <div className="flex h-11 flex-1 items-center rounded-full border border-white/50 px-4 text-[14px] text-white/85">{t.sendMessage}</div>
        <Heart className="h-7 w-7" strokeWidth={1.7} />
        <Send className="h-[26px] w-[26px]" strokeWidth={1.7} />
      </div>
    </div>
  );
}
