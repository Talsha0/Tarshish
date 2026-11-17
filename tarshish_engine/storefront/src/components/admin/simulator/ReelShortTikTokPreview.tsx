"use client";

// Full-screen 9:16 vertical video with the chrome of the platform that will show it:
// Instagram Reels, TikTok or YouTube Shorts. Right-hand engagement stack, spinning audio disc,
// bottom-left handle + expandable caption, and each app's bottom navigation. The chrome keeps
// physical positions (the apps do not mirror it); only the caption text follows the language.

import { motion } from "framer-motion";
import {
  Bookmark,
  Camera,
  Clapperboard,
  Ellipsis,
  Heart,
  House,
  Inbox,
  MessageCircle,
  MessageSquareText,
  Music2,
  Plus,
  Repeat2,
  Search,
  Send,
  Share2,
  SquarePlay,
  SquarePlus,
  ThumbsDown,
  ThumbsUp,
  User,
  Users,
  type LucideIcon,
} from "lucide-react";

import { chromeFor, compact, engagement, type PreviewItem, type VerticalVariant } from "./model";
import { Avatar, PreviewMedia } from "./parts";
import { RichText, clip } from "./RichText";
import { SAFE_TOP } from "./screen";

type RailAction = { icon: LucideIcon; label: string; fill?: boolean };
/** Bottom navigation height, home-indicator band included. */
const NAV_H = 82;

function Disc({ square }: { square: boolean }) {
  return (
    <motion.span
      className={`flex h-10 w-10 items-center justify-center overflow-hidden border-[3px] border-zinc-700 bg-gradient-to-br from-zinc-800 to-black ${
        square ? "rounded-lg border-2 border-white" : "rounded-full"
      }`}
      animate={square ? undefined : { rotate: 360 }}
      transition={{ duration: 4, ease: "linear", repeat: Infinity }}
    >
      {square ? <Avatar size={34} /> : <span className="h-5 w-5 rounded-full bg-[#0f6e52] ring-2 ring-[#ffd23f]" />}
    </motion.span>
  );
}

export function ReelShortTikTokPreview({
  item,
  text,
  expanded,
  onToggleCaption,
}: {
  item: PreviewItem;
  text: string;
  expanded: boolean;
  onToggleCaption: () => void;
}) {
  const variant: VerticalVariant = item.variant ?? "tiktok";
  const t = chromeFor(item.language);
  const stats = engagement(item.id);
  const shown = expanded ? { text, clipped: false } : clip(text, 70);
  const handle = item.handle.replace(/^@/, "");
  const tiktok = variant === "tiktok";
  const shorts = variant === "youtube_short";
  // #Shorts / #FindHub tags the platform shows under the caption when the text has none.
  const tags = [shorts && !/#shorts\b/i.test(text) ? "#Shorts" : null, !/#findhub\b/i.test(text) ? "#FindHub" : null].filter(Boolean).join(" ");

  const rail: RailAction[] = shorts
    ? [
        { icon: ThumbsUp, label: compact(stats.likes), fill: true },
        { icon: ThumbsDown, label: "Dislike", fill: true },
        { icon: MessageSquareText, label: compact(stats.comments), fill: true },
        { icon: Share2, label: "Share", fill: true },
        { icon: Repeat2, label: "Remix" },
      ]
    : tiktok
      ? [
          { icon: Heart, label: compact(stats.likes), fill: true },
          { icon: MessageCircle, label: compact(stats.comments), fill: true },
          { icon: Bookmark, label: compact(stats.saves), fill: true },
          { icon: Share2, label: compact(stats.shares), fill: true },
        ]
      : [
          { icon: Heart, label: compact(stats.likes) },
          { icon: MessageCircle, label: compact(stats.comments) },
          { icon: Send, label: compact(stats.shares) },
          { icon: Ellipsis, label: "" },
        ];

  return (
    <div className="relative h-full overflow-hidden bg-black text-white">
      <div className="absolute inset-0">
        <PreviewMedia item={item} />
      </div>
      <div className="pointer-events-none absolute inset-x-0 top-0 h-36 bg-gradient-to-b from-black/50 to-transparent" />
      <div className="pointer-events-none absolute inset-x-0 bottom-0 h-[55%] bg-gradient-to-t from-black/85 via-black/35 to-transparent" />

      {/* Top chrome */}
      <div className="absolute inset-x-0 flex items-center px-4" style={{ top: SAFE_TOP + 2 }} dir="ltr">
        {tiktok ? (
          <>
            <span className="w-6" />
            <div className="flex flex-1 items-center justify-center gap-5 text-[16px] font-semibold drop-shadow">
              <span className="text-white/65">Following</span>
              <span className="relative">
                For You
                <span className="absolute -bottom-1.5 left-1/2 h-[3px] w-7 -translate-x-1/2 rounded-full bg-white" />
              </span>
            </div>
            <Search className="h-6 w-6 drop-shadow" />
          </>
        ) : shorts ? (
          <>
            <span className="text-[17px] font-bold drop-shadow">Shorts</span>
            <span className="ms-auto flex gap-5">
              <Search className="h-6 w-6 drop-shadow" />
              <Ellipsis className="h-6 w-6 rotate-90 drop-shadow" />
            </span>
          </>
        ) : (
          <>
            <span className="text-[22px] font-bold drop-shadow">Reels</span>
            <Camera className="ms-auto h-7 w-7 drop-shadow" strokeWidth={1.75} />
          </>
        )}
      </div>

      {/* Right-hand engagement stack */}
      <div className="absolute right-2.5 flex flex-col items-center gap-[18px]" style={{ bottom: NAV_H + (tiktok ? 56 : 48) }}>
        {tiktok && (
          <span className="relative mb-2">
            <span className="block rounded-full border-2 border-white">
              <Avatar size={44} />
            </span>
            <span className="absolute -bottom-2 left-1/2 flex h-5 w-5 -translate-x-1/2 items-center justify-center rounded-full bg-[#fe2c55]">
              <Plus className="h-3.5 w-3.5" strokeWidth={3} />
            </span>
          </span>
        )}
        {rail.map(({ icon: Icon, label, fill }, index) => (
          <span key={index} className="flex flex-col items-center gap-1">
            <span className={shorts ? "flex h-12 w-12 items-center justify-center rounded-full bg-white/15 backdrop-blur" : ""}>
              <Icon
                className={`${tiktok ? "h-9 w-9" : shorts ? "h-6 w-6" : "h-7 w-7"} drop-shadow`}
                fill={fill && tiktok ? "currentColor" : "none"}
                strokeWidth={tiktok ? 1.25 : 1.9}
              />
            </span>
            {label && <span className="text-[12px] font-semibold drop-shadow">{label}</span>}
          </span>
        ))}
        <Disc square={!tiktok} />
      </div>

      {/* Bottom-left: account, caption, tags, audio */}
      <div className="absolute left-3 right-[72px] space-y-2" style={{ bottom: NAV_H + (shorts ? 14 : 8) }} dir={item.dir}>
        <div className="flex items-center gap-2">
          {!tiktok && <Avatar size={32} />}
          <bdi className="text-[15px] font-bold drop-shadow">{tiktok ? handle : `@${handle}`}</bdi>
          {!tiktok && (
            <span
              className={`rounded-full px-3 py-1 text-[12.5px] font-semibold ${shorts ? "bg-white text-black" : "border border-white/80 text-white"}`}
            >
              {shorts ? t.subscribe : t.follow}
            </span>
          )}
        </div>
        <div onClick={onToggleCaption} className={`cursor-pointer ${expanded ? "no-scrollbar max-h-[220px] overflow-y-auto" : ""}`}>
          <RichText
            text={shown.text}
            dir={item.dir}
            tone={{ hashtag: "font-semibold text-white", link: "text-white underline", cta: "bg-white/20 text-white" }}
            className="text-[14px] leading-[1.35] drop-shadow"
          />
          {shown.clipped && <span className="text-[14px] font-semibold text-white/80"> … {t.more}</span>}
          {tags && <p className="mt-1 text-[14px] font-semibold drop-shadow">{tags}</p>}
        </div>
        <div className="flex items-center gap-1.5 overflow-hidden text-[13px]">
          <Music2 className="h-3.5 w-3.5 shrink-0" />
          <div className="relative min-w-0 flex-1 overflow-hidden whitespace-nowrap">
            <motion.span
              className="inline-block"
              animate={{ x: item.dir === "rtl" ? ["0%", "40%"] : ["0%", "-40%"] }}
              transition={{ duration: 7, ease: "linear", repeat: Infinity }}
            >
              {t.originalAudio} · {handle} · {t.originalAudio} · {handle}
            </motion.span>
          </div>
        </div>
      </div>

      {/* Shorts progress line */}
      {shorts && (
        <div className="absolute inset-x-0 h-[3px] bg-white/25" style={{ bottom: NAV_H }}>
          <motion.div
            key={item.key}
            className="h-full bg-[#ff0033]"
            initial={{ width: "0%" }}
            animate={{ width: "100%" }}
            transition={{ duration: 15, ease: "linear", repeat: Infinity }}
          />
        </div>
      )}

      {/* Bottom navigation */}
      <div
        className={`absolute inset-x-0 bottom-0 flex items-start justify-around pt-2 ${tiktok || shorts ? "bg-black" : "bg-gradient-to-t from-black to-black/0"}`}
        style={{ height: NAV_H }}
        dir="ltr"
      >
        {tiktok ? (
          <>
            <NavItem icon={House} label="Home" active />
            <NavItem icon={Users} label="Friends" />
            <span className="mt-0.5 flex h-8 w-12 items-center justify-center rounded-lg bg-white shadow-[-3px_0_0_#25f4ee,3px_0_0_#fe2c55]">
              <Plus className="h-5 w-5 text-black" strokeWidth={3} />
            </span>
            <NavItem icon={Inbox} label="Inbox" />
            <NavItem icon={User} label="Profile" />
          </>
        ) : shorts ? (
          <>
            <NavItem icon={House} label="Home" />
            <NavItem icon={SquarePlay} label="Shorts" active />
            <span className="flex h-9 w-9 items-center justify-center rounded-full border border-white/40">
              <Plus className="h-6 w-6" strokeWidth={1.5} />
            </span>
            <NavItem icon={Clapperboard} label="Subscriptions" />
            <span className="flex flex-col items-center gap-0.5 text-[10px]">
              <Avatar size={24} />
              You
            </span>
          </>
        ) : (
          <>
            <House className="h-6 w-6" strokeWidth={1.8} />
            <Search className="h-6 w-6" strokeWidth={1.8} />
            <SquarePlus className="h-6 w-6" strokeWidth={1.8} />
            <Clapperboard className="h-6 w-6" strokeWidth={2.4} />
            <Avatar size={24} />
          </>
        )}
      </div>
    </div>
  );
}

function NavItem({ icon: Icon, label, active = false }: { icon: LucideIcon; label: string; active?: boolean }) {
  return (
    <span className={`flex flex-col items-center gap-0.5 text-[10px] ${active ? "font-semibold text-white" : "text-white/70"}`}>
      <Icon className="h-6 w-6" strokeWidth={active ? 2.3 : 1.7} fill={active ? "currentColor" : "none"} />
      {label}
    </span>
  );
}
