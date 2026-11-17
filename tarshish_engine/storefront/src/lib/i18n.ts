// Languages, direction and every word the storefront shows.
//
// Hebrew (`/he`) is laid out right-to-left and is the default; English (`/en`) is
// left-to-right. Components read their texts with `getDictionary(lang)`; nothing
// user-visible is hard-coded elsewhere. Layout uses logical CSS (start/end), so the
// same markup serves both directions.

export const LANGS = ["he", "en"] as const;
export type Lang = (typeof LANGS)[number];
export const DEFAULT_LANG: Lang = "he";

export function isLang(value: string): value is Lang {
  return (LANGS as readonly string[]).includes(value);
}

export function dirOf(lang: Lang): "rtl" | "ltr" {
  return lang === "he" ? "rtl" : "ltr";
}

export function localeOf(lang: Lang): string {
  return lang === "he" ? "he-IL" : "en-US";
}

/** `/he/c/sports` -> `/en/c/sports`: the same page in the other language. */
export function switchLangPath(pathname: string, target: Lang): string {
  const parts = pathname.split("/");
  if (parts[1] && isLang(parts[1])) {
    parts[1] = target;
    return parts.join("/") || `/${target}`;
  }
  return `/${target}`;
}

const en = {
  langName: "English",
  otherLangName: "עברית",
  meta: {
    title: "FindHub: checked deals from AliExpress and Temu",
    description:
      "An independent catalogue of AliExpress and Temu deals. Only products with a high rating and a proven order count are listed.",
    hubTitle: (name: string) => `${name} deals, checked | FindHub`,
  },
  nav: {
    home: "FindHub home",
    skip: "Skip to the deals",
    searchLabel: "Search deals",
    searchPlaceholder: "Search deals: earbuds, air fryer, running shoes",
    searchIn: (name: string) => `Search in ${name}`,
    clearSearch: "Clear search",
    language: "Language",
    saved: "Saved deals",
    savedCount: (count: number) => `Saved deals: ${count}`,
    darkMode: "Dark mode",
  },
  hero: {
    headline: "Deals worth the click.",
    blurb: (rating: string, orders: string) =>
      `FindHub tracks AliExpress and Temu and lists only products rated ${rating} or higher with at least ${orders} orders. Real prices, real ratings, and you always see where a link leads.`,
    featured: "Biggest price drop right now",
    featuredIn: (name: string) => `Biggest price drop in ${name}`,
    viewDeal: "See the deal",
  },
  categories: {
    all: "All",
    allDeals: "All deals",
    more: "More collections",
    names: { sports: "Sports", gadgets: "Gadgets", home: "Home" } as Record<string, string>,
    hubs: {
      sports: {
        title: "Sports gear that earned its stars",
        blurb: "Running, training and recovery picks, checked for rating and order count before they are listed.",
      },
      gadgets: {
        title: "Gadgets that hold up after unboxing",
        blurb: "Chargers, audio and desk tech, listed only while buyers keep rating them highly.",
      },
      home: {
        title: "Home upgrades at a fair price",
        blurb: "Kitchen and living-room helpers that passed the same rating and order checks.",
      },
    } as Record<string, { title: string; blurb: string }>,
    hubTitle: (name: string) => `${name} deals`,
    hubBlurb: "Every deal here passed the same rating and order checks.",
    deals: (count: string, n: number) => (n === 1 ? "1 deal" : `${count} deals`),
    checkedDeals: (count: string, n: number) => (n === 1 ? "1 checked deal" : `${count} checked deals`),
  },
  sort: {
    label: "Sort by",
    options: {
      recommended: "Recommended",
      relevance: "Best match",
      rating_desc: "Highest rated",
      price_asc: "Price: low to high",
      price_desc: "Price: high to low",
      orders_desc: "Most ordered",
      discount_desc: "Biggest discount",
    } as Record<string, string>,
  },
  filters: {
    title: "Filters",
    open: (active: number) => (active > 0 ? `Filters (${active})` : "Filters"),
    store: "Store",
    anyStore: "All stores",
    rating: "Rating",
    anyRating: "Any rating",
    ratingFrom: (rating: string) => `${rating} and up`,
    orders: "Orders",
    anyOrders: "Any number",
    ordersFrom: (orders: string) => `${orders}+ orders`,
    price: (symbol: string) => `Price (${symbol})`,
    priceMin: "From",
    priceMax: "To",
    clear: "Clear filters",
    show: (count: string, n: number) => (n === 1 ? "Show 1 deal" : `Show ${count} deals`),
    close: "Close",
  },
  card: {
    orders: (count: string) => `${count} orders`,
    ratingAria: (rating: string) => `Rated ${rating} out of 5`,
    sale: "SALE",
    was: (price: string) => `Was ${price}`,
    now: (price: string) => `Now ${price}`,
    save: "Save deal",
    unsave: "Remove from saved deals",
    getDeal: "Get deal",
    quickView: (title: string) => `Quick view: ${title}`,
    badges: {
      crowd_favorite: "Crowd Favorite",
      flash_deal: "Flash Deal",
      verified: "Algorithm Verified",
    },
    verifiedHint: (rating: string, orders: string) =>
      `Passed our automatic checks: rated ${rating} or higher, with at least ${orders} orders.`,
    social: {
      high_demand: "High demand this week",
      top_rated: "Top rated in category",
      most_ordered: "Most ordered in category",
    },
    urgency: { "Limited Stock": "Limited stock", "Top 1% Deal": "Top 1% deal" } as Record<string, string>,
  },
  quickView: {
    close: "Close",
    image: (index: number, total: number) => `Image ${index} of ${total}`,
    ratingOutOf: (rating: string) => `${rating} out of 5`,
    ordersOn: (orders: string, store: string) => `${orders} orders on ${store}`,
    soldOn: (store: string) => `Sold on ${store}`,
    save: (amount: string) => `You save ${amount}`,
    checksTitle: "What we checked",
    checkRating: (rating: string, minimum: string) => `Rated ${rating}. Our minimum is ${minimum}.`,
    checkOrders: (orders: string, minimum: string) => `${orders} orders. Our minimum is ${minimum}.`,
    checkLink: (store: string) => `The link opens this product's own page on ${store}.`,
    couponTitle: "Coupon code",
    couponHint: (store: string) => `Paste it at checkout on ${store}.`,
    copy: "Copy code",
    copied: "Copied!",
    cta: (store: string) => `Get Deal on ${store}`,
    priceNote: (store: string) => `${store} shows the final price, shipping and taxes at checkout.`,
    priceNoteConverted: (original: string, store: string) =>
      `Converted from ${original} at an approximate rate. ${store} shows the final price, shipping and taxes at checkout.`,
    share: "Copy link",
    linkCopied: "Link copied",
    loading: "Loading the deal",
    unavailable: "This deal is no longer listed.",
  },
  redirect: {
    title: (store: string) => `Continue to ${store}?`,
    bodyBefore: "You are leaving our curated catalog to view this verified deal directly on ",
    bodyAfter: ".",
    couponCopied: (code: string) => `Coupon ${code} is copied. Paste it at checkout.`,
    couponRemind: "Take your coupon with you:",
    confirm: "Continue to Store",
    opening: "Opening the store",
    cancel: "Cancel",
    newTab: "Opens in a new tab.",
    disclosure: "FindHub may earn a commission on this purchase. Your price stays the same.",
    failed: "The link could not be opened. Check your connection and try again.",
  },
  saved: {
    title: "Saved deals",
    emptyTitle: "No saved deals yet",
    emptyBody: "Tap the heart on a deal to keep it here. Saved deals stay on this device.",
    browse: "Browse deals",
    remove: "Remove",
    clear: "Remove all",
    device: "Kept on this device only.",
  },
  toast: {
    saved: "Added to Saved Deals",
    unsaved: "Removed from Saved Deals",
    coupon: "Coupon Copied",
    copyFailed: "Copying is blocked in this browser. Select the code and copy it by hand.",
  },
  results: {
    showing: (shown: string, total: string) => `Showing ${shown} of ${total}`,
    forQuery: (query: string) => `Results for “${query}”`,
    emptyTitle: "No deals match these filters",
    emptySearchTitle: (query: string) => `Nothing found for “${query}”`,
    emptyBody: "Try a shorter word, check the spelling, or clear the filters.",
    errorTitle: "The deals could not be loaded",
    errorBody: "The catalogue is not answering right now. Your saved deals are still here.",
    retry: "Try again",
    more: "Show more deals",
    loadingMore: "Loading more deals",
    loading: "Loading deals",
  },
  footer: {
    about: "FindHub is an independent deal catalogue. We are not AliExpress or Temu; we link to them.",
    disclosure:
      "Some links are affiliate links: we may earn a commission when you buy, at no extra cost to you.",
    prices: "Prices and stock change quickly. The store's own page always has the final word.",
    collections: "Collections",
  },
  notFound: {
    title: "This page is not here",
    body: "The link may be old, or the deal has ended.",
  },
};

export type Dictionary = typeof en;

const he: Dictionary = {
  langName: "עברית",
  otherLangName: "English",
  meta: {
    title: "FindHub: דילים בדוקים מ-AliExpress ומ-Temu",
    description:
      "קטלוג עצמאי של דילים מ-AliExpress ומ-Temu. מופיעים בו רק מוצרים עם דירוג גבוה ומספר הזמנות מוכח.",
    hubTitle: (name: string) => `דילים בדוקים בקטגוריית ${name} | FindHub`,
  },
  nav: {
    home: "FindHub, לעמוד הבית",
    skip: "דילוג אל הדילים",
    searchLabel: "חיפוש דילים",
    searchPlaceholder: "חיפוש דילים: אוזניות, סיר טיגון, נעלי ריצה",
    searchIn: (name: string) => `חיפוש בקטגוריית ${name}`,
    clearSearch: "ניקוי החיפוש",
    language: "שפה",
    saved: "דילים שמורים",
    savedCount: (count: number) => `דילים שמורים: ${count}`,
    darkMode: "מצב כהה",
  },
  hero: {
    headline: "דילים ששווה ללחוץ עליהם.",
    blurb: (rating: string, orders: string) =>
      `FindHub עוקב אחרי AliExpress ו-Temu ומציג רק מוצרים עם דירוג ${rating} ומעלה ולפחות ${orders} הזמנות. מחירים אמיתיים, דירוגים אמיתיים, ותמיד רואים לאן הקישור מוביל.`,
    featured: "ירידת המחיר הגדולה ביותר כרגע",
    featuredIn: (name: string) => `ירידת המחיר הגדולה בקטגוריית ${name}`,
    viewDeal: "לצפייה בדיל",
  },
  categories: {
    all: "הכול",
    allDeals: "כל הדילים",
    more: "קולקציות נוספות",
    names: { sports: "ספורט", gadgets: "גאדג׳טים", home: "בית" },
    hubs: {
      sports: {
        title: "ציוד ספורט שהרוויח את הכוכבים",
        blurb: "בחירות לריצה, לאימון ולהתאוששות, שנבדקו לפי דירוג ומספר הזמנות לפני שעלו לכאן.",
      },
      gadgets: {
        title: "גאדג׳טים שמחזיקים גם אחרי הפתיחה",
        blurb: "מטענים, אודיו וציוד לשולחן העבודה, שנשארים כאן רק כל עוד הקונים ממשיכים לדרג אותם גבוה.",
      },
      home: {
        title: "שדרוגים לבית במחיר הוגן",
        blurb: "עוזרים למטבח ולסלון שעברו את אותן בדיקות דירוג והזמנות.",
      },
    },
    hubTitle: (name: string) => `דילים בקטגוריית ${name}`,
    hubBlurb: "כל דיל כאן עבר את אותן בדיקות דירוג והזמנות.",
    deals: (count: string, n: number) => (n === 1 ? "דיל אחד" : `${count} דילים`),
    checkedDeals: (count: string, n: number) => (n === 1 ? "דיל בדוק אחד" : `${count} דילים בדוקים`),
  },
  sort: {
    label: "מיון לפי",
    options: {
      recommended: "מומלצים",
      relevance: "הכי רלוונטי",
      rating_desc: "הדירוג הגבוה ביותר",
      price_asc: "מחיר: מהנמוך לגבוה",
      price_desc: "מחיר: מהגבוה לנמוך",
      orders_desc: "הכי מוזמנים",
      discount_desc: "ההנחה הגדולה ביותר",
    },
  },
  filters: {
    title: "סינון",
    open: (active: number) => (active > 0 ? `סינון (${active})` : "סינון"),
    store: "חנות",
    anyStore: "כל החנויות",
    rating: "דירוג",
    anyRating: "כל דירוג",
    ratingFrom: (rating: string) => `${rating} ומעלה`,
    orders: "הזמנות",
    anyOrders: "כל כמות",
    ordersFrom: (orders: string) => `${orders}+ הזמנות`,
    price: (symbol: string) => `מחיר (${symbol})`,
    priceMin: "מ-",
    priceMax: "עד",
    clear: "ניקוי הסינון",
    show: (count: string, n: number) => (n === 1 ? "הצגת דיל אחד" : `הצגת ${count} דילים`),
    close: "סגירה",
  },
  card: {
    orders: (count: string) => `${count} הזמנות`,
    ratingAria: (rating: string) => `דירוג ${rating} מתוך 5`,
    sale: "מבצע",
    was: (price: string) => `מחיר קודם ${price}`,
    now: (price: string) => `מחיר נוכחי ${price}`,
    save: "שמירת הדיל",
    unsave: "הסרה מהדילים השמורים",
    getDeal: "לדיל",
    quickView: (title: string) => `צפייה מהירה: ${title}`,
    badges: {
      crowd_favorite: "בחירת הקהל",
      flash_deal: "מבצע בזק",
      verified: "נבדק ע״י המערכת",
    },
    verifiedHint: (rating: string, orders: string) =>
      `עבר את הבדיקות האוטומטיות שלנו: דירוג ${rating} ומעלה ולפחות ${orders} הזמנות.`,
    social: {
      high_demand: "ביקוש גבוה השבוע",
      top_rated: "הדירוג הגבוה בקטגוריה",
      most_ordered: "הכי מוזמן בקטגוריה",
    },
    urgency: { "Limited Stock": "מלאי מוגבל", "Top 1% Deal": "דיל מהאחוזון העליון" },
  },
  quickView: {
    close: "סגירה",
    image: (index: number, total: number) => `תמונה ${index} מתוך ${total}`,
    ratingOutOf: (rating: string) => `${rating} מתוך 5`,
    ordersOn: (orders: string, store: string) => `${orders} הזמנות ב-${store}`,
    soldOn: (store: string) => `נמכר ב-${store}`,
    save: (amount: string) => `חוסכים ${amount}`,
    checksTitle: "מה בדקנו",
    checkRating: (rating: string, minimum: string) => `דירוג ${rating}. המינימום שלנו הוא ${minimum}.`,
    checkOrders: (orders: string, minimum: string) => `${orders} הזמנות. המינימום שלנו הוא ${minimum}.`,
    checkLink: (store: string) => `הקישור פותח את עמוד המוצר עצמו ב-${store}.`,
    couponTitle: "קוד קופון",
    couponHint: (store: string) => `מדביקים אותו בקופה ב-${store}.`,
    copy: "העתקת הקוד",
    copied: "הועתק!",
    cta: (store: string) => `למעבר לדיל ב-${store}`,
    priceNote: (store: string) => `המחיר הסופי, כולל משלוח ומיסים, מוצג בקופה של ${store}.`,
    priceNoteConverted: (_original: string, store: string) =>
      `המחיר הסופי, כולל משלוח ומיסים, מוצג בקופה של ${store}.`,
    share: "העתקת קישור",
    linkCopied: "הקישור הועתק",
    loading: "טוענים את הדיל",
    unavailable: "הדיל הזה כבר לא מופיע אצלנו.",
  },
  redirect: {
    title: (store: string) => `ממשיכים ל-${store}?`,
    bodyBefore: "אתם יוצאים מהקטלוג שלנו כדי לצפות בדיל המאומת הזה ישירות באתר ",
    bodyAfter: ".",
    couponCopied: (code: string) => `הקופון ${code} הועתק. הדביקו אותו בקופה.`,
    couponRemind: "קחו איתכם את הקופון:",
    confirm: "המשך לחנות",
    opening: "פותחים את החנות",
    cancel: "ביטול",
    newTab: "נפתח בלשונית חדשה.",
    disclosure: "ייתכן ש-FindHub יקבל עמלה על הרכישה הזו. המחיר שלכם לא משתנה.",
    failed: "לא הצלחנו לפתוח את הקישור. בדקו את החיבור ונסו שוב.",
  },
  saved: {
    title: "דילים שמורים",
    emptyTitle: "עדיין אין דילים שמורים",
    emptyBody: "לחצו על הלב בדיל כדי לשמור אותו כאן. הדילים השמורים נשארים במכשיר הזה.",
    browse: "לכל הדילים",
    remove: "הסרה",
    clear: "הסרת הכול",
    device: "נשמר במכשיר הזה בלבד.",
  },
  toast: {
    saved: "נוסף לדילים השמורים",
    unsaved: "הוסר מהדילים השמורים",
    coupon: "הקופון הועתק",
    copyFailed: "הדפדפן חוסם העתקה. סמנו את הקוד והעתיקו אותו ידנית.",
  },
  results: {
    showing: (shown: string, total: string) => `מוצגים ${shown} מתוך ${total}`,
    forQuery: (query: string) => `תוצאות עבור ״${query}״`,
    emptyTitle: "אין דילים שמתאימים לסינון הזה",
    emptySearchTitle: (query: string) => `לא נמצאו תוצאות עבור ״${query}״`,
    emptyBody: "נסו מילה קצרה יותר, בדקו את האיות או נקו את הסינון.",
    errorTitle: "לא הצלחנו לטעון את הדילים",
    errorBody: "הקטלוג לא עונה כרגע. הדילים השמורים שלכם עדיין כאן.",
    retry: "ניסיון חוזר",
    more: "הצגת דילים נוספים",
    loadingMore: "טוענים דילים נוספים",
    loading: "טוענים דילים",
  },
  footer: {
    about: "FindHub הוא קטלוג דילים עצמאי. אנחנו לא AliExpress ולא Temu, אנחנו מקשרים אליהן.",
    disclosure: "חלק מהקישורים הם קישורי שותפים: ייתכן שנקבל עמלה על רכישה, בלי תוספת מחיר עבורכם.",
    prices: "מחירים ומלאי משתנים מהר. המילה האחרונה היא תמיד של עמוד החנות.",
    collections: "קולקציות",
  },
  notFound: {
    title: "העמוד הזה לא נמצא",
    body: "ייתכן שהקישור ישן או שהדיל הסתיים.",
  },
};

const dictionaries: Record<Lang, Dictionary> = { he, en };

export function getDictionary(lang: Lang): Dictionary {
  return dictionaries[lang];
}

/** Display name of a category: translated when known, otherwise its key made readable. */
export function categoryName(lang: Lang, key: string): string {
  const known = dictionaries[lang].categories.names[key];
  if (known) return known;
  const words = key.replace(/[-_]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Hub headline and blurb for a category, with a neutral fallback for new categories. */
export function hubCopy(lang: Lang, key: string): { title: string; blurb: string } {
  const t = dictionaries[lang].categories;
  return t.hubs[key] ?? { title: t.hubTitle(categoryName(lang, key)), blurb: t.hubBlurb };
}
