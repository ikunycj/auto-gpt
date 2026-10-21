# React WebUI

`web/` is the React/Vite frontend for the local Flask console. The backend API lives in `apps/web/app.py`. The React app calls same-origin `/api/*` routes so Flask session authentication and downloads keep working.

## Development

```bash
cd web
npm install
npm run dev
```

The Vite development server proxies `/api`, `/login`, and `/logout` to the WebUI listener at `http://127.0.0.1:5000`.

## Build

```bash
cd web
npm run build
```

The checked-in `web/dist/` output is served by Flask at `/`. Flask returns a clear `503` response when the bundle is missing; there is no separate template UI.

The production console is still started with the repository's single-listener command:

```bash
./webui.sh start
```
