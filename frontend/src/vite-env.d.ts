/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_WS_BASE_URL?: string;
  readonly VITE_GIT_SHA?: string;
  readonly VITE_API_SCHEMA?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
