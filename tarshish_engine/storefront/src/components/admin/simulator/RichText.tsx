"use client";

// Post text the way the apps render it: hashtags, @mentions and URLs coloured, and the
// "Link in bio" call to action highlighted. The paragraph direction follows the post's language
// (Hebrew posts read right-to-left), and URLs, hashtags, mentions and the leading handle are
// isolated with <bdi> so Latin runs inside Hebrew text keep their own order (#FindHub, not FindHub#).

import { Fragment, type ReactNode } from "react";

const TOKEN =
  /(https?:\/\/[^\s]+)|(#[\p{L}\p{M}\p{N}_\u05F3\u05F4]+)|(@[\p{L}\p{N}_.]+)|((?:🔗\s*)?(?:link in bio|הקישור בביו|לינק בביו|קישור בביו)[^\n]*)/giu;

export type RichTextTone = {
  hashtag: string;
  mention?: string;
  link: string;
  cta: string;
};

function tokens(text: string, tone: RichTextTone): ReactNode[] {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(TOKEN)) {
    const index = match.index ?? 0;
    if (index > last) parts.push(text.slice(last, index));
    const [value, url, tag, mention, cta] = match;
    if (url)
      parts.push(
        <bdi key={index} className={`${tone.link} break-all`}>
          {url}
        </bdi>,
      );
    else if (tag)
      parts.push(
        <bdi key={index} className={tone.hashtag}>
          {tag}
        </bdi>,
      );
    else if (mention)
      parts.push(
        <bdi key={index} className={tone.mention ?? tone.hashtag}>
          {mention}
        </bdi>,
      );
    else if (cta)
      parts.push(
        <mark key={index} className={`rounded px-1 font-semibold ${tone.cta}`}>
          {cta}
        </mark>,
      );
    last = index + value.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

export function RichText({
  text,
  dir,
  tone,
  className = "",
  lead,
}: {
  text: string;
  dir: "rtl" | "ltr";
  tone: RichTextTone;
  className?: string;
  /** Rendered inline before the text (the bold account handle on Instagram). */
  lead?: ReactNode;
}) {
  const lines = text.split("\n");
  return (
    <div dir={dir} className={`whitespace-pre-wrap break-words text-start ${className}`}>
      {lead && (
        <>
          <bdi>{lead}</bdi>{" "}
        </>
      )}
      {lines.map((line, index) => (
        <Fragment key={index}>
          {index > 0 && "\n"}
          {tokens(line, tone)}
        </Fragment>
      ))}
    </div>
  );
}

/** First `max` characters on whole-word boundaries, for the collapsed "… more" captions. */
export function clip(text: string, max: number): { text: string; clipped: boolean } {
  const firstLines = text.split("\n").slice(0, 2).join("\n");
  const base = firstLines.length < text.length ? firstLines : text;
  if (base.length <= max) return { text: base, clipped: base.length < text.length };
  const cut = base.slice(0, max);
  const space = cut.lastIndexOf(" ");
  return { text: (space > max * 0.6 ? cut.slice(0, space) : cut).trimEnd(), clipped: true };
}
