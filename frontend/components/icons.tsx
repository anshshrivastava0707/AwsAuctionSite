// Small stroke icons, sized by font-size and colored by currentColor.

type Props = { size?: number; className?: string };

const base = (size: number) => ({
  width: size,
  height: size,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
});

export const SearchIcon = ({ size = 20, className }: Props) => (
  <svg {...base(size)} className={className}><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
);

export const ChevronDown = ({ size = 16, className }: Props) => (
  <svg {...base(size)} className={className}><path d="m6 9 6 6 6-6" /></svg>
);

export const ClockIcon = ({ size = 18, className }: Props) => (
  <svg {...base(size)} className={className}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>
);

export const HeartIcon = ({ size = 20, className, filled }: Props & { filled?: boolean }) => (
  <svg {...base(size)} className={className} fill={filled ? "currentColor" : "none"}>
    <path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10Z" />
  </svg>
);

/** BidBloom mark: four petals around a center. */
export const BloomLogo = ({ size = 32 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden>
    <g fill="var(--accent)">
      <circle cx="16" cy="8.5" r="6.5" />
      <circle cx="23.5" cy="16" r="6.5" />
      <circle cx="16" cy="23.5" r="6.5" />
      <circle cx="8.5" cy="16" r="6.5" />
    </g>
    <circle cx="16" cy="16" r="3.2" fill="var(--surface)" />
  </svg>
);
