"use client";

// A modern smartphone (375×812 points) that renders one unpublished post with the skin of the
// platform it will go to. It scales itself down to fit the space it is given, slides between
// items, and shows a progress overlay while a publish or delete is in flight.

import { AnimatePresence, motion } from "framer-motion";
import { BatteryFull, LoaderCircle, Signal, Wifi } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { InstagramPostPreview } from "./InstagramPostPreview";
import { InstagramStoryPreview } from "./InstagramStoryPreview";
import type { PreviewItem } from "./model";
import type { Appearance } from "./parts";
import { ReelShortTikTokPreview } from "./ReelShortTikTokPreview";
import { BEZEL, SCREEN_H, SCREEN_W } from "./screen";
import { TelegramPostPreview } from "./TelegramPostPreview";

const FRAME_W = SCREEN_W + BEZEL * 2;
const FRAME_H = SCREEN_H + BEZEL * 2;

export type SimulatorBusy = { kind: "publishing" | "deleting" | "saving"; label: string } | null;

function useClock(): string {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const timer = window.setInterval(() => setNow(new Date()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  return now ? now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : "9:41";
}

/** Largest scale (≤ maxScale) at which the frame fits the element's box. */
function useFitScale(maxScale: number) {
  const ref = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(0.7);
  useLayoutEffect(() => {
    const box = ref.current;
    if (!box) return;
    const measure = () => {
      const { width, height } = box.getBoundingClientRect();
      if (!width || !height) return;
      setScale(Math.max(0.35, Math.min(maxScale, width / FRAME_W, height / FRAME_H)));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    return () => observer.disconnect();
  }, [maxScale]);
  return { ref, scale };
}

function StatusBar({ light }: { light: boolean }) {
  const clock = useClock();
  return (
    <div
      className={`pointer-events-none absolute inset-x-0 top-0 z-30 flex h-[50px] items-center justify-between px-7 pt-1 text-[15px] font-semibold ${
        light ? "text-white" : "text-black"
      }`}
      dir="ltr"
    >
      <span className="w-[70px] text-center tabular-nums">{clock}</span>
      <span className="flex w-[70px] items-center justify-center gap-1">
        <Signal className="h-[15px] w-[15px]" strokeWidth={2.6} />
        <Wifi className="h-[15px] w-[15px]" strokeWidth={2.6} />
        <BatteryFull className="h-[18px] w-[22px]" strokeWidth={2} />
      </span>
    </div>
  );
}

export function MobileDeviceSimulator({
  item,
  text,
  appearance = "light",
  expanded,
  onToggleCaption,
  busy = null,
  direction = 1,
  maxScale = 1,
  className = "",
}: {
  item: PreviewItem;
  /** Display text: the (possibly edited) caption plus whatever the publisher appends. */
  text: string;
  appearance?: Appearance;
  expanded: boolean;
  onToggleCaption: () => void;
  busy?: SimulatorBusy;
  /** Slide direction of the transition: 1 = next, -1 = previous. */
  direction?: 1 | -1;
  maxScale?: number;
  className?: string;
}) {
  const { ref, scale } = useFitScale(maxScale);
  const fullBleed = item.layout === "instagram_story" || item.layout === "vertical_video";
  const lightStatus = fullBleed || appearance === "dark";

  let screen;
  switch (item.layout) {
    case "instagram_post":
      screen = <InstagramPostPreview item={item} text={text} appearance={appearance} expanded={expanded} onToggleCaption={onToggleCaption} />;
      break;
    case "instagram_story":
      screen = <InstagramStoryPreview item={item} />;
      break;
    case "vertical_video":
      screen = <ReelShortTikTokPreview item={item} text={text} expanded={expanded} onToggleCaption={onToggleCaption} />;
      break;
    default:
      screen = <TelegramPostPreview item={item} text={text} appearance={appearance} />;
  }

  return (
    <div ref={ref} className={`flex h-full w-full items-center justify-center ${className}`}>
      <div style={{ width: FRAME_W * scale, height: FRAME_H * scale }} className="relative shrink-0">
        <div
          className="absolute left-0 top-0 origin-top-left"
          style={{ width: FRAME_W, height: FRAME_H, transform: `scale(${scale})` }}
          role="img"
          aria-label={`${item.typeLabel} preview for ${item.handle}`}
        >
          {/* Hardware: titanium band, side buttons, shadow */}
          <div className="absolute inset-0 rounded-[60px] bg-gradient-to-br from-zinc-600 via-zinc-800 to-zinc-700 p-[3px] shadow-[0_40px_80px_-30px_rgba(0,0,0,0.6),0_18px_36px_-18px_rgba(0,0,0,0.45)]">
            <div className="h-full w-full rounded-[57px] bg-black" />
          </div>
          <span className="absolute -left-[3px] top-[118px] h-8 w-[4px] rounded-l bg-zinc-700" />
          <span className="absolute -left-[3px] top-[178px] h-14 w-[4px] rounded-l bg-zinc-700" />
          <span className="absolute -left-[3px] top-[246px] h-14 w-[4px] rounded-l bg-zinc-700" />
          <span className="absolute -right-[3px] top-[200px] h-20 w-[4px] rounded-r bg-zinc-700" />

          {/* Screen */}
          <div
            className={`absolute overflow-hidden rounded-[48px] ${appearance === "dark" || fullBleed ? "bg-black" : "bg-white"}`}
            style={{ left: BEZEL, top: BEZEL, width: SCREEN_W, height: SCREEN_H }}
            lang={item.language}
          >
            <AnimatePresence initial={false} custom={direction} mode="popLayout">
              <motion.div
                key={item.key}
                custom={direction}
                className="absolute inset-0"
                variants={{
                  enter: (dir: number) => ({ x: dir * 60, opacity: 0 }),
                  center: { x: 0, opacity: 1 },
                  exit: (dir: number) => ({ x: dir * -60, opacity: 0 }),
                }}
                initial="enter"
                animate="center"
                exit="exit"
                transition={{ duration: 0.22, ease: "easeOut" }}
              >
                {screen}
              </motion.div>
            </AnimatePresence>

            <StatusBar light={lightStatus} />
            {/* Dynamic Island */}
            <span className="pointer-events-none absolute left-1/2 top-[11px] z-30 h-[35px] w-[122px] -translate-x-1/2 rounded-full bg-black" />
            {/* Home indicator */}
            <span
              className={`pointer-events-none absolute bottom-2 left-1/2 z-30 h-[5px] w-[134px] -translate-x-1/2 rounded-full ${
                lightStatus ? "bg-white/90" : "bg-black/85"
              }`}
            />

            <AnimatePresence>
              {busy && (
                <motion.div
                  className="absolute inset-0 z-40 flex flex-col items-center justify-center gap-3 bg-black/60 text-white backdrop-blur-[2px]"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                >
                  <LoaderCircle className="h-10 w-10 animate-spin" />
                  <p className="px-8 text-center text-[15px] font-semibold">{busy.label}</p>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </div>
      </div>
    </div>
  );
}
