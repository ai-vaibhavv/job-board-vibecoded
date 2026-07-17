// TypeScript mirror of the API's JSON shapes. Kept in step with the Pydantic
// `Job` model (src/job_alerts/models.py) and the service dicts in
// dashboard/service.py.

export type JobStatus = "new" | "notified" | "rejected";
export type Language = "en" | "de" | "unknown";
export type RemoteStatus = "on_site" | "hybrid" | "remote" | "unknown";

/** Compact per-job payload for cards (from `list_jobs_json`). */
export interface JobSummary {
  id: string;
  title: string;
  organization: string | null;
  location: string | null;
  city: string | null;
  country: string | null;
  source: string;
  language: Language;
  status: JobStatus;
  relevance_score: number;
  score_color: string;
  matched_keywords: string[];
  remote_status: RemoteStatus;
  published_at: string | null;
  notified_at: string | null;
  url: string;
  logo: string | null;
  hidden: boolean;
}

/** The full Job dump, plus the two fields the API adds. */
export interface JobFull {
  id: string;
  source: string;
  source_job_id: string | null;
  title: string;
  organization: string | null;
  location: string | null;
  country: string | null;
  city: string | null;
  remote_status: RemoteStatus;
  description: string | null;
  url: string;
  contact_email: string | null;
  contact_url: string | null;
  published_at: string | null;
  discovered_at: string;
  enriched_at: string | null;
  application_deadline: string | null;
  employment_type: string | null;
  language: Language;
  salary: string | null;
  relevance_score: number;
  matched_keywords: string[];
  score_explanation: string[];
  card_summary: string | null;
  content_hash: string;
  notified_at: string | null;
  status: JobStatus;
  logo: string | null;
  score_color: string;
}

export interface Translation {
  description_en: string;
  card_summary_en: string | null;
  truncated: boolean;
}

export type LinkStatus = "alive" | "moved" | "dead" | "unverifiable" | null;

export interface JobDetail {
  exists: boolean;
  job: JobFull;
  translation: Translation | null;
  is_german: boolean;
  translation_unavailable: boolean;
  needs_confirm: boolean;
  confirm_label: string;
  rejection_reason: string | null;
  hidden: boolean;
  link_status: LinkStatus;
  apply_url: string;
  alternate_links: string[];
  detail_fetched_at: string | null;
}

export interface SecretStatus {
  set: boolean;
  hint: string;
}

export interface SettingsStatus {
  secrets: Record<string, SecretStatus>;
  search_api_provider: string;
  colab_base_url: string;
  providers: string[];
}

export interface Stats {
  total_jobs: number;
  by_status: Record<string, number>;
  by_source: Record<string, number>;
  average_score: number | null;
  notified: number;
  database_path: string;
}

export interface Meta {
  topics: string[];
  locations: string[];
  sources: string[];
  stats: Stats;
}

export interface Health {
  status: string;
  llm_online: boolean;
}

export interface JobsResponse {
  jobs: JobSummary[];
  total: number;
}

export interface SearchTask {
  task_id: string;
  status: "running" | "done" | "error";
  result: string | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface SearchPreview {
  queries: string;
  scope: string;
}

export interface ResumeResult {
  keywords: string;
  topics: string[];
  message: string;
}

export interface JobFilters {
  status: string; // "all" | JobStatus
  min_score: number;
  source: string; // "all" | source name
  text: string;
  show_hidden: boolean;
}
