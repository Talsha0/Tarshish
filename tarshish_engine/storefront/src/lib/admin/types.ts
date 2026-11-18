// Shapes returned by the FastAPI admin API (storefront_api/admin.py).

export type EnvStatus = "Configured" | "Missing";
export type JobStatus = "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED";
export type PubStatus = "QUEUED" | "PENDING" | "PUBLISHED" | "FAILED";
export type Platform = "instagram" | "tiktok" | "youtube" | "telegram";

export type Job = {
  id: string;
  kind: string;
  status: JobStatus;
  exit_code: number | null;
  error: string | null;
  params: Record<string, unknown>;
  command: string;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  log: string[];
  log_total: number;
  done: boolean;
};

export type Provider = {
  order: number;
  name: string;
  host: string;
  adapter: string;
  enabled: boolean;
  api_key_env: string;
  key_status: "Ready" | "Missing";
  key_detail: string;
  last_outcome: string | null;
  last_detail: string | null;
};

export type SourceChain = {
  store: string;
  config_path: string | null;
  from_file: boolean;
  providers: Provider[];
  ready: boolean;
  error: string | null;
  last_run: {
    finished_at?: string;
    succeeded?: string | null;
    category?: string;
    keyword?: string;
    usable?: number;
  } | null;
};

export type Overview = {
  kpis: {
    storefront_clicks: number;
    storefront_clicks_7d: number;
    published_posts: number;
    active_products: number;
    harvested_products: number;
  };
  budget: { spent_usd: number; limit_usd: number; ratio: number; state: "ok" | "warning" | "critical" | "exceeded" };
  queue: Record<PubStatus, number>;
  categories: { total: number; active: number };
  social_accounts: { total: number; enabled: number; ready: number; by_platform: Record<Platform, number> };
  sources: { temu: SourceChain; aliexpress: SourceChain };
  platform_credentials: {
    platform: Platform;
    available: boolean;
    env: { credential: string; env_var: string; status: EnvStatus; required: boolean }[];
  }[];
  environment: { group: string; vars: { name: string; purpose: string; status: EnvStatus }[] }[];
  recent_jobs: Job[];
  runtime: { database: string; serverless: boolean; background_jobs: boolean; plugin_load_errors: string[] };
};

export type Category = {
  category_id: string;
  display_name_en: string;
  display_name_he: string;
  temu_search: Record<string, unknown> | null;
  aliexpress_search: Record<string, unknown> | null;
  min_rating: number;
  min_orders: number;
  enabled: boolean;
  sources: string[];
  products: Record<string, number>;
  accounts: number;
};

export type AccountEnv = {
  credential: string;
  env_var: string | null;
  status: EnvStatus;
  required: boolean;
  inline_from: string | null;
  explicit: boolean;
};

export type SocialAccount = {
  id: string;
  category_id: string;
  language: string;
  platform: string;
  page_handle: string;
  token_env_var: string | null;
  account_id_env_var: string | null;
  platform_metadata: Record<string, string>;
  enabled: boolean;
  publisher: string;
  status: "ready" | "missing_credentials" | "disabled" | "no_publisher";
  reason: string | null;
  env: AccountEnv[];
};

export type PlatformFields = {
  label: string;
  handle_label: string;
  handle_placeholder: string;
  metadata: { key: string; label: string; required: boolean }[];
  token_label: string;
  token_placeholder: string;
  account_id_label?: string;
  account_id_placeholder?: string;
};

export type VerifyResult = {
  account: SocialAccount;
  ok: boolean;
  summary: string;
  lines: string[];
  checked_at?: string;
};

export type ContentProfile = {
  content_type: string;
  platform: string;
  generator: string;
  llm_model: string;
  publish_format: string | null;
  tone: string | null;
  prompt_template: string | null;
  enabled: boolean;
  model_label: string;
  accounts: number;
};

export type ContentProfiles = {
  profiles: ContentProfile[];
  models: { id: string; label: string; provider: string }[];
  generators: string[];
  formats: string[];
  provider_keys: { anthropic: EnvStatus; gemini: EnvStatus };
};

export type Publication = {
  id: number;
  pipeline_id: string;
  category: string | null;
  language: string | null;
  content_format: string | null;
  account_platform: string | null;
  platform: string;
  target_page: string;
  content_type: string;
  status: PubStatus;
  product_title: string;
  caption: string;
  media_uri: string | null;
  model_used: string;
  generation_cost_usd: number;
  platform_post_id: string | null;
  scheduled_for: string | null;
  published_at: string | null;
  error_message: string | null;
  attempts: number;
  created_at: string;
};

// Generated FindHub assets (GET /api/admin/assets).
export type AssetStatus = "DRAFT" | "QUEUED" | "PUBLISHED" | "FAILED";
export type AssetContentType = "telegram_post" | "instagram_post" | "instagram_story" | "instagram_reel" | "tiktok_post";

export type StoryLink = { kind: "storefront" | "product" | "affiliate"; label: string; url: string };

export type AssetLinks = {
  affiliate_url: string;
  storefront_product_url: string;
  storefront_category_url: string;
  cta:
    | { kind: "telegram_links"; links: string[] }
    | { kind: "link_in_bio"; text: string; bio_link: string }
    | { kind: "link_stickers" };
  story_links?: StoryLink[];
  story_links_applied?: boolean;
  story_links_note?: string;
  reel_caption?: string;
  llm_failover?: string[];
};

export type GeneratedAsset = {
  id: number;
  product_id: number;
  category_id: string;
  language: string;
  platform: "telegram" | "instagram" | "tiktok";
  content_type: AssetContentType;
  target_account_id: string | null;
  target_account_handle: string | null;
  generated_text: string;
  generated_image_path: string | null;
  generated_image_url: string | null;
  llm_model_used: string;
  status: AssetStatus;
  platform_post_id: string | null;
  error_message: string | null;
  attempts: number;
  published_at: string | null;
  created_at: string;
  updated_at: string | null;
  product_title: string;
  product_title_he: string | null;
  product_source: string;
  product_price: number;
  product_original_price: number | null;
  product_currency: string;
  product_rating: number | null;
  product_orders_count: number | null;
  product_image_url: string | null;
  links: AssetLinks;
  publishing: boolean;
  publishable: boolean;
  not_publishable_reason: string | null;
};

export type FreeLLMLink = {
  provider: "gemini" | "groq" | "openrouter";
  model: string;
  label: string;
  env_var: string;
  configured: boolean;
  cooling_down_seconds: number;
  last_problem: string | null;
};

export type AssetsResponse = {
  items: GeneratedAsset[];
  counts: Record<AssetStatus, number>;
  llm_chain: FreeLLMLink[];
  storefront_base_url: string;
};
