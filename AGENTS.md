# WebUI Runtime

- This repository may run only one WebUI listener: `http://127.0.0.1:5000`.
- Do not start `web.py` on `5001`, `5002`, or any other port unless the user explicitly changes this rule.
- Before starting or restarting, check for an existing WebUI process. Do not leave multiple WebUI processes reading and writing the same workspace data files.
- Use `./webui.sh start`, `./webui.sh stop`, or `./webui.sh restart` with the default `PORT=5000`; do not override `PORT`.
