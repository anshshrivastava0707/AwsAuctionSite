import Link from "next/link";
import AuctionGrid from "@/components/AuctionGrid";
import Dropdown from "@/components/Dropdown";
import { ChevronDown, ClockIcon, SearchIcon } from "@/components/icons";
import { listAuctions } from "@/lib/api";
import { CATEGORIES, type Auction, type Category } from "@/lib/types";

// Always render from the database at request time.
export const dynamic = "force-dynamic";

// The first few get their own chip; the rest live under "More".
const PRIMARY: Category[] = ["ART", "COLLECTIBLES", "ELECTRONICS", "FASHION", "HOME_GARDEN", "SPORTS"];
const MORE = (Object.keys(CATEGORIES) as Category[]).filter((c) => !PRIMARY.includes(c));
const CHIP_LABEL: Partial<Record<Category, string>> = { HOME_GARDEN: "Home" };
const chipLabel = (c: Category) => CHIP_LABEL[c] ?? CATEGORIES[c];

const SORTS = {
  ending: "Ending soon",
  newest: "Newly listed",
  price_low: "Price: low to high",
  price_high: "Price: high to low",
} as const;
type Sort = keyof typeof SORTS;

const price = (a: Auction) => a.currentHigh ?? a.startingPrice;

// Live auctions closing first, then upcoming by start, then finished (most recent first).
function endingRank(a: Auction): [number, number] {
  if (a.phase === "LIVE") return [0, a.endsAt];
  if (a.phase === "SCHEDULED") return [1, a.startsAt];
  return [2, -a.endsAt];
}

function sortAuctions(list: Auction[], sort: Sort): Auction[] {
  const out = [...list];
  if (sort === "ending") {
    out.sort((x, y) => {
      const [rx, kx] = endingRank(x);
      const [ry, ky] = endingRank(y);
      return rx - ry || kx - ky;
    });
  }
  if (sort === "price_low") out.sort((x, y) => price(x) - price(y));
  if (sort === "price_high") out.sort((x, y) => price(y) - price(x));
  return out; // "newest" is the API's own order
}

function matches(a: Auction, q: string): boolean {
  const hay = [a.title, a.description, CATEGORIES[a.category], a.sellerName ?? ""].join(" ").toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((word) => hay.includes(word));
}

type Params = { category?: string; q?: string; sort?: string };

export default async function Home({ searchParams }: { searchParams: Promise<Params> }) {
  const params = await searchParams;
  const category = params.category && params.category in CATEGORIES ? (params.category as Category) : null;
  const sort: Sort = params.sort && params.sort in SORTS ? (params.sort as Sort) : "ending";
  const q = (params.q ?? "").trim();

  let auctions: Auction[] = [];
  let error: string | null = null;
  try {
    auctions = sortAuctions(await listAuctions(category), sort);
  } catch (e) {
    error = e instanceof Error ? e.message : "Failed to load auctions";
  }
  if (q) auctions = auctions.filter((a) => matches(a, q));

  const href = (change: Params) => {
    const next = { category: category ?? undefined, q: q || undefined, sort: sort === "ending" ? undefined : sort, ...change };
    const qs = new URLSearchParams(Object.entries(next).filter((e): e is [string, string] => !!e[1])).toString();
    return qs ? `/?${qs}` : "/";
  };
  const moreActive = category != null && MORE.includes(category);

  return (
    <div className="explore">
      <section className="hero">
        <h1>Find your next favorite.</h1>
        <p>One-of-a-kind finds, going once.</p>
      </section>

      <form className="searchbar" action="/" role="search">
        <SearchIcon size={22} />
        <input
          id="search"
          name="q"
          type="search"
          defaultValue={q}
          placeholder="Search auctions, brands, categories…"
          aria-label="Search auctions"
        />
        {category && <input type="hidden" name="category" value={category} />}
        {sort !== "ending" && <input type="hidden" name="sort" value={sort} />}
        <button type="submit">Search</button>
      </form>

      <div className="filters">
        <div className="chips">
          <Link href={href({ category: undefined })} className={`chip ${category ? "" : "active"}`}>All</Link>
          {PRIMARY.map((c) => (
            <Link key={c} href={href({ category: c })} className={`chip ${category === c ? "active" : ""}`}>
              {chipLabel(c)}
            </Link>
          ))}
          <Dropdown
            triggerClass={`chip ${moreActive ? "active" : ""}`}
            trigger={<>{moreActive ? chipLabel(category!) : "More"} <ChevronDown /></>}
          >
            {MORE.map((c) => (
              <Link key={c} role="menuitem" href={href({ category: c })} className={category === c ? "active" : undefined}>
                {chipLabel(c)}
              </Link>
            ))}
          </Dropdown>
        </div>
        <Dropdown
          align="right"
          label="Sort auctions"
          triggerClass="sort-trigger"
          trigger={<><ClockIcon /> {SORTS[sort]} <ChevronDown /></>}
        >
          {(Object.keys(SORTS) as Sort[]).map((s) => (
            <Link key={s} role="menuitem" href={href({ sort: s === "ending" ? undefined : s })} className={s === sort ? "active" : undefined}>
              {SORTS[s]}
            </Link>
          ))}
        </Dropdown>
      </div>

      {error && <p className="notice bad">{error}</p>}
      {!error && auctions.length === 0 && (
        <div className="empty-state">
          {q ? <>No auctions match “{q}”. <Link href={href({ q: undefined })}>Clear search</Link></> : "No auctions here yet."}
        </div>
      )}
      <AuctionGrid auctions={auctions} />
    </div>
  );
}
