import { BrandIcon } from "../api";
import { useCatalog } from "../catalog";

/** A site's logo in a rounded tile. The drawing comes from config, through the
 *  API: this file knows no site. Without one, the tile shows an initial, so an
 *  Actor that declares no logo still lines up with those that do. */
export function PlatformLogo({
  icon, name, size = 28,
}: { icon?: BrandIcon | null; name: string; size?: number }) {
  const glyph = Math.round(size * 0.6);
  return (
    <span className="logo-tile" style={{ width: size, height: size, borderRadius: Math.round(size * 0.28) }}
          aria-hidden>
      {icon ? (
        <svg width={glyph} height={glyph} viewBox="0 0 24 24" role="presentation">
          <path d={icon.d} fill={icon.color ?? "currentColor"} />
        </svg>
      ) : (
        <span style={{ fontSize: Math.round(size * 0.45), fontWeight: 700 }}>
          {name.slice(0, 1).toUpperCase()}
        </span>
      )}
    </span>
  );
}

/** Logo + the label a person reads, for anything that names an Actor or a
 *  platform. The raw name stays available as the tooltip. */
export function BrandName({
  icon, label, name, size = 24, strong = true,
}: { icon?: BrandIcon | null; label?: string | null; name: string; size?: number; strong?: boolean }) {
  return (
    <span className="brand-name" title={label && label !== name ? name : undefined}>
      <PlatformLogo icon={icon} name={label || name} size={size} />
      {strong ? <strong>{label || name}</strong> : <span>{label || name}</span>}
    </span>
  );
}

/** An Actor by name (a run's `actor`), resolved through the shared catalog. */
export function ActorName({ name, size, strong }: { name: string; size?: number; strong?: boolean }) {
  const a = useCatalog().actor(name);
  return <BrandName icon={a?.icon} label={a?.label} name={name} size={size} strong={strong} />;
}

/** A platform by name (a connected account's `platform`). */
export function PlatformName({ name, size, strong }: { name: string; size?: number; strong?: boolean }) {
  const p = useCatalog().platform(name);
  return <BrandName icon={p?.icon} label={p?.label} name={name} size={size} strong={strong} />;
}
