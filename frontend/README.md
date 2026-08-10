# Analytics Copilot — frontend

Vite + React + TypeScript chat client. `npm run dev` calls the API at the relative `/api`
prefix (`src/api.ts`); Vite's dev server proxies `/api` to `http://127.0.0.1:8000`
(`vite.config.ts`), so the backend must be running there. `npm test -- --run`; `npm run build`.
