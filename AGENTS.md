# WebUI Runtime

- This repository uses exactly two local WebUI listeners: the frontend at `http://127.0.0.1:5555` and the Flask backend at `http://127.0.0.1:6666`.
- Do not start either service on any other port unless the user explicitly changes this rule.
- Before starting or restarting, check for existing frontend and backend processes. Do not leave multiple backend processes reading and writing the same workspace data files.
- Use `./webui.sh start`, `./webui.sh stop`, or `./webui.sh restart`; the frontend and backend ports are fixed and must not be overridden.
