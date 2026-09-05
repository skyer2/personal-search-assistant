function stripTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function isLocalHost(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1";
}

function isRemoteBrowser(): boolean {
  return typeof window !== "undefined" && !isLocalHost(window.location.hostname);
}

function sameOriginWsBaseUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}`;
}

/** 远程浏览器访问时，自动修正 env 里误配的 localhost */
function fixLocalhostForRemoteBrowser(httpOrWsUrl: string, fallback: string): string {
  if (!isRemoteBrowser()) {
    return httpOrWsUrl;
  }

  try {
    const normalized = httpOrWsUrl.startsWith("ws")
      ? httpOrWsUrl.replace(/^ws(s?):\/\//, "http$1://")
      : httpOrWsUrl;
    const parsed = new URL(normalized);
    if (isLocalHost(parsed.hostname)) {
      console.warn(
        `[config] 检测到远程访问但 endpoint 指向 localhost (${httpOrWsUrl})，已自动切换为 ${fallback}`
      );
      return fallback;
    }
  } catch {
    return httpOrWsUrl;
  }

  return httpOrWsUrl;
}

function deriveWsBaseUrl(apiBaseUrl: string): string {
  if (import.meta.env.VITE_WS_BASE_URL) {
    return stripTrailingSlash(import.meta.env.VITE_WS_BASE_URL);
  }

  // 开发模式下未显式配置时，走 Vite 代理（/ws -> 后端），避免远程浏览器误连 localhost
  if (import.meta.env.DEV && !import.meta.env.VITE_API_BASE_URL) {
    return sameOriginWsBaseUrl();
  }

  if (apiBaseUrl.startsWith("https://")) {
    return apiBaseUrl.replace(/^https:\/\//, "wss://");
  }

  if (apiBaseUrl.startsWith("http://")) {
    return apiBaseUrl.replace(/^http:\/\//, "ws://");
  }

  return sameOriginWsBaseUrl();
}

function resolveApiBaseUrl(): string {
  if (import.meta.env.VITE_API_BASE_URL) {
    return stripTrailingSlash(import.meta.env.VITE_API_BASE_URL);
  }

  // 开发模式下走 Vite 同源代理（/api -> 后端）
  if (import.meta.env.DEV) {
    return "";
  }

  // Production is always same-origin. Reverse proxies own /api and /ws.
  return "";
}

const resolvedApiBaseUrl = resolveApiBaseUrl();
const devProxyApiBaseUrl = "";

/** HTTP API 根地址；开发代理模式下为空字符串，请求应使用相对路径 */
export const API_BASE_URL = fixLocalhostForRemoteBrowser(
  resolvedApiBaseUrl || devProxyApiBaseUrl,
  devProxyApiBaseUrl
);

export const WS_BASE_URL = fixLocalhostForRemoteBrowser(
  deriveWsBaseUrl(resolvedApiBaseUrl),
  sameOriginWsBaseUrl()
);

export function apiUrl(path: string): string {
  return API_BASE_URL ? `${API_BASE_URL}${path}` : path;
}

export function buildApiUrl(path: string, params?: Record<string, string>): URL {
  const url = API_BASE_URL
    ? new URL(`${API_BASE_URL}${path}`)
    : new URL(path, window.location.origin);

  if (params) {
    Object.entries(params).forEach(([key, value]) => {
      url.searchParams.set(key, value);
    });
  }

  return url;
}

export function wsUrl(threadId: string): string {
  return `${WS_BASE_URL}/ws/${encodeURIComponent(threadId)}`;
}
