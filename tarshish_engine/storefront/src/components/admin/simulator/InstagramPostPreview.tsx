"use client";

// Instagram feed post, as an account's post opens on a phone: header, 4:5 image, action row,
// likes, caption with the bold handle, comments link and the bottom tab bar.

import { Bookmark, ChevronLeft, Clapperboard, Ellipsis, Heart, House, MessageCircle, Search, Send, SquarePlus } from "lucide-react";

import { chromeFor, engagement, type PreviewItem } from "./model";
import { Avatar, PreviewMedia, type Appearance } from "./parts";
import { RichText, clip } from "./RichText";
import { SAFE_TOP } from "./screen";

export function InstagramPostPreview({
  item,
  text,
  appearance,
  expanded,
  onToggleCaption,
}: {
  item: PreviewItem;
  text: string;
  appearance: Appearance;
  expanded: boolean;
  onToggleCaption: () => void;
}) {
  const dark = appearance === "dark";
  const t = chromeFor(item.language);
  const stats = engagement(item.id);
  const shown = expanded ? { text, clipped: false } : clip(text, 90);
  const ink = dark ? "text-white" : "text-[#0c1014]";
  const soft = dark ? "text-[#a8a8a8]" : "text-[#737373]";
  const tone = {
    hashtag: dark ? "text-[#e0f1ff]" : "text-[#00376b]",
    link: dark ? "text-[#e0f1ff]" : "text-[#00376b]",
    cta: dark ? "bg-[#0095f6]/25 text-[#8fd0ff]" : "bg-[#0095f6]/12 text-[#0064b0]",
  };

  return (
    <div className={`flex h-full flex-col ${dark ? "bg-black" : "bg-white"} ${ink}`} style={{ paddingTop: SAFE_TOP }}>
      {/* App header: the account's posts */}
      <div className="flex h-11 shrink-0 items-center gap-3 px-3">
        <ChevronLeft className="h-7 w-7" strokeWidth={1.75} />
        <div className="flex-1 text-center leading-tight">
          <p className={`text-[11px] font-semibold uppercase ${soft}`}>{item.handle.replace(/^@/, "")}</p>
          <p className="text-[15px] font-bold">{t.posts}</p>
        </div>
        <span className="w-7" />
      </div>

      <div className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
        {/* Post header */}
        <div className="flex items-center gap-2.5 px-3 py-2">
          <Avatar size={32} ring />
          <div className="min-w-0 flex-1 leading-tight">
            <p className="truncate text-[13.5px] font-semibold">{item.handle.replace(/^@/, "")}</p>
            <p className={`truncate text-[11.5px] ${dark ? "text-[#f5f5f5]" : "text-[#0c1014]"}`}>FindHub · {item.categoryId}</p>
          </div>
          <Ellipsis className="h-5 w-5" />
        </div>

        {/* 4:5 branded image */}
        <div className="aspect-[4/5] w-full overflow-hidden bg-[#efefef]">
          <PreviewMedia item={item} />
        </div>

        {/* Actions */}
        <div className="flex items-center gap-4 px-3 pb-1.5 pt-2.5">
          <Heart className="h-[25px] w-[25px]" strokeWidth={1.8} />
          <MessageCircle className="h-[25px] w-[25px] -scale-x-100" strokeWidth={1.8} />
          <Send className="h-[23px] w-[23px]" strokeWidth={1.8} />
          <Bookmark className="ms-auto h-[25px] w-[25px]" strokeWidth={1.8} />
        </div>

        <div className="space-y-1 px-3 pb-6 text-[13.5px] leading-[1.35]">
          <p className="font-semibold" dir={item.dir}>
            {t.likes(stats.likes.toLocaleString("en"))}
          </p>
          <div onClick={onToggleCaption} className="cursor-pointer">
            <RichText
              text={shown.text}
              dir={item.dir}
              tone={tone}
              lead={<span className="font-semibold">{item.handle.replace(/^@/, "")}</span>}
            />
            {shown.clipped && <span className={soft}>… {t.more}</span>}
          </div>
          <p className={`text-[13.5px] ${soft}`} dir={item.dir}>
            {t.viewComments(stats.comments)}
          </p>
          <p className={`text-[11px] ${soft}`} dir={item.dir}>
            {t.hoursAgo}
          </p>
        </div>
      </div>

      {/* Tab bar */}
      <div className={`flex h-[80px] shrink-0 items-start justify-around border-t pt-2.5 ${dark ? "border-white/10 bg-black" : "border-black/5 bg-white"}`}>
        <House className="h-6 w-6" strokeWidth={2.1} />
        <Search className="h-6 w-6" strokeWidth={1.8} />
        <SquarePlus className="h-6 w-6" strokeWidth={1.8} />
        <Clapperboard className="h-6 w-6" strokeWidth={1.8} />
        <Avatar size={24} />
      </div>
    </div>
  );
}
