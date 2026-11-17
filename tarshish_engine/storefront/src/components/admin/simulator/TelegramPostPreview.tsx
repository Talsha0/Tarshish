"use client";

// Telegram channel post: channel header, wallpaper, the post bubble (image attachment or link
// preview, formatted text, views and time), inline URL buttons and the subscriber's "Mute" bar.

import { ArrowUpRight, BellOff, ChevronLeft, Eye, Search } from "lucide-react";

import { chromeFor, compact, engagement, type PreviewItem } from "./model";
import { Avatar, PreviewMedia, type Appearance } from "./parts";
import { RichText } from "./RichText";
import { SAFE_TOP } from "./screen";

function domain(url: string | null): string {
  if (!url) return "findhub";
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return "findhub";
  }
}

export function TelegramPostPreview({ item, text, appearance }: { item: PreviewItem; text: string; appearance: Appearance }) {
  const dark = appearance === "dark";
  const t = chromeFor(item.language);
  const stats = engagement(item.id);
  // A generated Telegram asset is a text post: Telegram shows a preview of its first link.
  const linkPreview = item.source === "asset" && item.contentType === "telegram_post";
  const buttons = [
    { label: t.viewOnFindHub, url: item.links.productUrl ?? item.links.storefrontUrl },
    { label: t.directDeal, url: item.links.affiliateUrl },
  ].filter((button): button is { label: string; url: string } => Boolean(button.url));
  const time = new Date(item.createdAt);
  const clock = Number.isNaN(time.getTime()) ? "14:32" : time.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });

  const wallpaper = dark
    ? "radial-gradient(circle at 20% 20%, #1d3a32 0, transparent 45%), radial-gradient(circle at 80% 70%, #18302b 0, transparent 50%), #0f1c19"
    : "radial-gradient(circle at 15% 20%, #d5e6b4 0, transparent 45%), radial-gradient(circle at 85% 65%, #b6d39b 0, transparent 50%), radial-gradient(circle at 50% 100%, #8fbf8a 0, transparent 55%), #c7dca8";

  return (
    <div className={`flex h-full flex-col ${dark ? "text-white" : "text-black"}`}>
      {/* Channel header */}
      <div className={`shrink-0 border-b ${dark ? "border-white/10 bg-[#1c1c1d]" : "border-black/10 bg-[#f6f6f6]"}`} style={{ paddingTop: SAFE_TOP }}>
        <div className="flex h-12 items-center gap-2 px-2" dir="ltr">
          <ChevronLeft className="h-7 w-7 text-[#2a9ef1]" />
          <div className="flex-1 text-center leading-tight">
            <p className="text-[16px] font-semibold">FindHub Deals</p>
            <p className={`text-[12.5px] ${dark ? "text-white/55" : "text-black/50"}`}>{t.subscribers(stats.subscribers.toLocaleString("en"))}</p>
          </div>
          <Avatar size={36} />
        </div>
      </div>

      {/* Chat */}
      <div className="no-scrollbar relative min-h-0 flex-1 overflow-y-auto px-2.5 pb-3 pt-2" style={{ background: wallpaper }}>
        <div className="mb-2 flex justify-center">
          <span className="rounded-full bg-black/25 px-2.5 py-0.5 text-[12px] font-medium text-white">{t.today}</span>
        </div>

        <div className="max-w-[92%]">
          <div className={`overflow-hidden rounded-2xl rounded-bl-md shadow-sm ${dark ? "bg-[#212b2a]" : "bg-white"}`}>
            {!linkPreview && (
              <div className="aspect-[4/5] max-h-[330px] w-full overflow-hidden bg-zinc-200">
                <PreviewMedia item={item} />
              </div>
            )}
            <div className="px-3 pb-1.5 pt-2">
              <p className="mb-0.5 text-[14px] font-semibold text-[#3a8fd6]">FindHub Deals</p>
              <RichText
                text={text}
                dir={item.dir}
                tone={{ hashtag: "text-[#168acd]", link: "text-[#168acd]", cta: "bg-[#168acd]/10 text-[#168acd]" }}
                className="text-[15px] leading-[1.38]"
              />
              {linkPreview && (
                <div className="mt-2 flex gap-2 rounded-md border-s-[3px] border-[#3a8fd6] bg-[#3a8fd6]/10 p-2">
                  <div className="min-w-0 flex-1">
                    <p className="text-[13px] font-semibold text-[#3a8fd6]">{domain(item.links.affiliateUrl)}</p>
                    <p className="line-clamp-2 text-[13px] font-semibold" dir="auto">
                      {item.productTitle}
                    </p>
                  </div>
                  <div className="h-14 w-14 shrink-0 overflow-hidden rounded">
                    <PreviewMedia item={item} />
                  </div>
                </div>
              )}
              <div className={`mt-0.5 flex items-center justify-end gap-1 text-[11.5px] ${dark ? "text-white/45" : "text-black/40"}`} dir="ltr">
                <Eye className="h-3.5 w-3.5" />
                {compact(stats.views)}
                <span className="ms-1">{clock}</span>
              </div>
            </div>
          </div>

          {/* Inline URL buttons */}
          {buttons.length > 0 && (
            <div className="mt-1 grid grid-cols-2 gap-1" dir={item.dir}>
              {buttons.map((button) => (
                <a
                  key={button.label}
                  href={button.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  title={button.url}
                  className="relative flex h-10 items-center justify-center rounded-lg bg-black/30 px-3 text-[14px] font-medium text-white backdrop-blur-sm transition hover:bg-black/40"
                >
                  <span className="truncate">{button.label}</span>
                  <ArrowUpRight className="absolute end-1 top-1 h-3 w-3 opacity-80" />
                </a>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Subscriber bar */}
      <div
        className={`flex h-[78px] shrink-0 items-start justify-between border-t px-4 pt-3 ${dark ? "border-white/10 bg-[#1c1c1d]" : "border-black/10 bg-[#f6f6f6]"}`}
        dir="ltr"
      >
        <Search className="h-6 w-6 text-[#2a9ef1]" />
        <span className="flex items-center gap-1.5 text-[16px] font-medium text-[#2a9ef1]">
          <BellOff className="h-4 w-4" /> {t.mute}
        </span>
        <span className="w-6" />
      </div>
    </div>
  );
}
