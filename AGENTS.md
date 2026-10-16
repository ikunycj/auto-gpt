# WebUI Runtime

- This repository uses exactly two local WebUI listeners: the frontend at `http://127.0.0.1:5555` and the Flask backend at `http://127.0.0.1:6666`.
- Do not start either service on any other port unless the user explicitly changes this rule.
- Before starting or restarting, check for existing frontend and backend processes. Do not leave multiple backend processes reading and writing the same workspace data files.
- Use `./webui.sh start`, `./webui.sh stop`, or `./webui.sh restart`; the frontend and backend ports are fixed and must not be overridden.

## Local Audit Output

- This service is intended for local use and is not exposed externally. During local diagnosis, code review, testing, and runtime verification, do not mask or redact data in logs, error messages, command output, API responses, or audit summaries; preserve complete values so the behavior can be reviewed accurately.
- Apply redaction only when output is being shared outside this local environment or when the user explicitly requests redaction.

## Engineering Scope

- 当前服务仅本地开发运行，没有任何历史逻辑，不需要兼容或者迁移，改动代码以工程最佳为准，不需要写向前兼容的代码。
- 不要新增旧数据迁移、历史集合回补、兼容字段或启动时自动恢复旧结构的逻辑；数据模型变更直接修改当前实现和当前测试。
