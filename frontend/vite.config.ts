/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The API base URL is read from VITE_API_BASE_URL at build time (see src/api/client.ts), not
// proxied here: the production build is static files on a different host from the API, so a
// dev-only proxy would hide the CORS and absolute-URL problems until deployment day.
export default defineConfig({
  plugins: [react()],
  server: {
    // Matches the backend's default CORS_ORIGINS, so a fresh checkout works with no config.
    port: 5173,
    strictPort: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
})
