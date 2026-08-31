# React WebUI

`web/` is the React/Vite frontend for the local Flask console. The frontend is
fixed at `http://127.0.0.1:5555`; the backend API in `apps/web/app.py` is fixed
at `http://127.0.0.1:6666`. Vite dev and preview proxy `/api` to the backend,
so the React app keeps using same-origin requests. The local console has no
authorization-code login gate.

## Development

先在项目根目录同步 Python 后端依赖。后端环境由 uv 管理；前端需要
Node.js `20.19.0+`（20.x）或 `22.12.0+`，与锁定的 Vite 8.2.2 要求一致。

```bash
uv sync --locked
cd web
npm ci
npm run dev
```

`npm run dev` 固定在 `http://127.0.0.1:5555` 启动 Vite，并将 `/api`
代理到 `http://127.0.0.1:6666` 上的 Flask 后端。

## Build

```bash
cd web
npm run build
npm run preview
```

`npm run preview` 在 `http://127.0.0.1:5555` 提供 `web/dist/`，并使用与 dev
相同的代理规则访问 `http://127.0.0.1:6666` 后端。

生产控制台仍由仓库管理脚本启动；它会同时管理 `5555` 前端和 `6666` 后端：

```bash
./webui.sh start
```

`webui.sh` 面向 macOS/Linux；Windows 或不使用 shell 管理脚本时，在项目根目录前台运行后端：

```bash
uv run --locked python web.py
```

该命令只启动 `http://127.0.0.1:6666` 后端；另一个终端需在 `web/` 目录
运行 `npm run dev` 或 `npm run preview`，浏览器访问
`http://127.0.0.1:5555`。
