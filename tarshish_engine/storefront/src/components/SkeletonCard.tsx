// Loading placeholder with the exact anatomy of ProductCard (photo, rating row, two title
// lines, badge row, discount row, price label, button), so the grid does not jump when the
// real cards arrive. The shimmer is CSS only and stops for reduced-motion users.

export function SkeletonCard() {
  return (
    <div aria-hidden="true" className="flex h-full flex-col overflow-hidden rounded-[14px] border border-line bg-surface shadow-card">
      <div className="skeleton aspect-square !rounded-none" />
      <div className="flex flex-1 flex-col gap-2.5 p-3 sm:p-4">
        <div className="flex items-center gap-2">
          <div className="skeleton h-5 w-11" />
          <div className="skeleton h-3.5 w-20" />
        </div>
        <div className="flex flex-col gap-1.5 py-0.5">
          <div className="skeleton h-3.5 w-full" />
          <div className="skeleton h-3.5 w-3/4" />
        </div>
        <div className="flex items-center gap-2.5">
          <div className="skeleton h-5 w-24 !rounded-full" />
          <div className="skeleton h-3.5 w-16" />
        </div>
        <div className="mt-auto flex flex-col gap-2.5 pt-1">
          <div className="flex items-center gap-2">
            <div className="skeleton h-5 w-16" />
            <div className="skeleton h-3.5 w-12" />
          </div>
          <div className="skeleton h-7 w-28" />
          <div className="skeleton h-10 w-full !rounded-[10px]" />
        </div>
      </div>
    </div>
  );
}

export const GRID_CLASS = "grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-3 xl:grid-cols-4";

export function SkeletonGrid({ count = 8, label }: { count?: number; label: string }) {
  return (
    <div role="status" aria-label={label} className={GRID_CLASS}>
      {Array.from({ length: count }, (_, index) => (
        <SkeletonCard key={index} />
      ))}
    </div>
  );
}
