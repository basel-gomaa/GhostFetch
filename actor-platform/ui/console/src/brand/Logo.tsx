/**
 * The GhostFetch mark, hand-traced from docs/brand/ghostfetch-brand-reference.jpg
 * (master copy: docs/brand/ghostfetch-mark.svg). The top stream line IS the
 * ghost's outline and the third line is its tail, so the paths below must stay
 * as they are -- redrawing them "cleaner" is how a logo stops being the logo.
 *
 * Colour comes from `currentColor`, so the mark follows the theme: purple on
 * light, lifted purple on dark, white on the purple sign-in panel.
 */

const VIEW = { x: 6, y: 10, w: 182, h: 93 };

export function GhostMark({
  size = 40,
  animated = false,
  title,
  className,
}: {
  /** Rendered width in px; the height follows the mark's own proportions. */
  size?: number;
  /** Streams the data lines (loading states). Decorative only. */
  animated?: boolean;
  /** Accessible name. Omitted, the mark is decorative and hidden from readers. */
  title?: string;
  className?: string;
}) {
  const stream = (n: string) => (animated ? `gm-stream ${n}` : undefined);
  return (
    <svg
      className={className}
      width={size}
      height={Math.round((size * VIEW.h) / VIEW.w)}
      viewBox={`${VIEW.x} ${VIEW.y} ${VIEW.w} ${VIEW.h}`}
      fill="none"
      stroke="currentColor"
      strokeWidth={7.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
    >
      {/* The top stream line and the ghost's outline are ONE stroke in the
          logo. They are drawn as two paths meeting at the dip, both with a
          level tangent there, so the static mark is unchanged while the
          stream half can animate without dashing the ghost's body. */}
      <path className={stream("s0")} pathLength={animated ? 88 : undefined} d="M34 37.5H81C92 37.5 98 58.5 113 58.5" />
      <path d="M113 58.5C126 58.5 131 40 138 29C145 18 152 15.5 160 15.5C172 15.5 182 26 182 42C182 62 172 80 156 88C145 94 132 97 122 97C114 97 109 94 106 91" />
      <path className={stream("s1")} pathLength={animated ? 110 : undefined} d="M12 51.5H76C86 51.5 92 70 104 71.5C114 72.5 120 71 127 68" />
      <path className={stream("s2")} pathLength={animated ? 110 : undefined} d="M24 65.5H68C80 65.5 88 88 104 90C116 91 128 84 136 79" />
      <path className={stream("s3")} pathLength={animated ? 44 : undefined} d="M37 79H74" />
      <g fill="currentColor" stroke="none">
        <ellipse cx="152.5" cy="40" rx="4.4" ry="5.6" transform="rotate(-10 152.5 40)" />
        <ellipse cx="167" cy="46" rx="3.8" ry="4.8" transform="rotate(-10 167 46)" />
      </g>
    </svg>
  );
}

/** Mark + name, as on the brand sheet. `size` is the height of the name. */
export function Wordmark({ size = 20, animated = false }: { size?: number; animated?: boolean }) {
  return (
    <span className="wordmark" aria-label="GhostFetch">
      <GhostMark size={Math.round(size * 2.1)} animated={animated} />
      <span className="wordmark-text" style={{ fontSize: size }} aria-hidden>GhostFetch</span>
    </span>
  );
}
