export type Call = {
  call_sid: string;
  from_number: string | null;
  language: string | null;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
  captured_name: string | null;
  captured_phone: string | null;
  captured_email: string | null;
  transcript:
    | { role: "user" | "assistant"; text: string; ts: string }[]
    | null;
  status: string | null;
  summary: string | null;
  created_at: string;
};
