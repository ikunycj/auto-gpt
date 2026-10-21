# React WebUI

`web/` is the React/Vite frontend for the local Flask console. React Router provides URL-based navigation; the existing page components continue to use the backend API in `apps/web/app.py`. Same-origin `/api/*` routes preserve Flask session authentication and downloads.

## Development

```bash
cd web
npm install
npm run dev
```

The Vite development server listens on `http://127.0.0.1:5555` and proxies `/api`, `/login`, and `/logout` to Flask at `http://127.0.0.1:6666`.

## Build

```bash
cd web
npm run build
```

The checked-in `web/dist/` output is served by Vite preview at `http://127.0.0.1:5555/`; it proxies API and authentication routes to Flask at `http://127.0.0.1:6666`. React Router URLs resolve to the same app entry.

The production console starts both fixed listeners with:

```bash
./webui.sh start
```
