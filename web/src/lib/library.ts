/** Library tab: the user's saved references (screenshots and links), uploaded and browsed with their Tailscale login. */

export type RefStatus = "inbox" | "reviewed";

export interface RefImage {
  sha256: string;
  width: number;
  height: number;
}

export interface RefCard {
  id: number;
  title: string;
  status: RefStatus;
  category: string | null;
  category_auto: boolean;
  note: string;
  description: string;
  url: string;
  saved: string;
  reviewed: string | null;
  images: RefImage[];
}

export interface RefDetail extends RefCard {
  ocr: string[];
}

export interface RefList {
  refs: RefCard[];
  counts: Record<RefStatus, number>;
}

export const mediaUrl = (sha: string, size: "thumb" | "view" | "full") => `/api/v1/web/media/${sha}?size=${size}`;

async function json<T>(res: Response): Promise<T> {
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.message ?? body.error ?? `HTTP ${res.status}`);
  return body as T;
}

export async function listRefs(status: RefStatus | "all", q: string, signal?: AbortSignal): Promise<RefList> {
  const params = new URLSearchParams();
  if (status !== "all") params.set("status", status);
  if (q.trim()) params.set("q", q.trim());
  return json(await fetch(`/api/v1/web/refs?${params}`, { cache: "no-store", signal }));
}

export async function getRef(id: number): Promise<RefDetail> {
  return json(await fetch(`/api/v1/web/refs/${id}`, { cache: "no-store" }));
}

export async function deleteRef(id: number): Promise<void> {
  await json(await fetch(`/api/v1/web/refs/${id}`, { method: "DELETE" }));
}

export interface CaptureResult {
  id: number;
  merged: boolean;
  images: number;
  ocr_chars: number;
  message: string;
}

export async function capture(body: { image?: string; url?: string; note?: string; group?: string }): Promise<CaptureResult> {
  return json(
    await fetch("/api/v1/capture/ref", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

/** Longest edge sent to the server: an iPhone screenshot (1290 x 2796) passes untouched, so OCR keeps every pixel. */
const MAX_EDGE = 2800;

/**
 * Any image the browser can show (including iPhone HEIC) as base64 JPEG, scaled down if larger than MAX_EDGE.
 * The server accepts PNG/JPEG/WebP/GIF only, and a JPEG of a screenshot is a fraction of the PNG's size.
 */
export async function toJpegBase64(file: Blob): Promise<string> {
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.decoding = "async";
    img.src = url;
    await img.decode();
    const scale = Math.min(1, MAX_EDGE / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = Math.round(img.naturalHeight * scale);
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("This browser cannot convert images.");
    ctx.fillStyle = "#fff"; // transparent PNGs get a white page, not black
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob | null>((ok) => canvas.toBlob(ok, "image/jpeg", 0.9));
    if (!blob) throw new Error("Could not encode the image.");
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(binary);
  } finally {
    URL.revokeObjectURL(url);
  }
}

export const shareGroup = () => `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

export function prettyDate(iso: string): string {
  const d = new Date(iso);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", ...(sameYear ? {} : { year: "numeric" }) });
}

export function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}
