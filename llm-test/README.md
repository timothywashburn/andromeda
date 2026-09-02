# llm-test/

Working files for the LLM host on VM 100 (`testing`, 10.127.0.100).
The system described here is **built and running**.

| File | Status | Purpose |
|---|---|---|
| `integration-plan.md` | proposal | How to fold the built system into the playbooks. As-built state, corrected estimates, gotchas, and context-scaling guidance. |
| `docker-compose.yml` | **deployed** | Mirrors `/srv/llm/docker-compose.yml` on VM 100. Edit here, copy across. |
| `opencode.json` | client config | Copy to `~/.config/opencode/opencode.json`. |

## Access
- **Open WebUI** — http://10.127.0.100:8080 (admin account already created)
- **OpenAI-compatible API** — http://10.127.0.100:8081/v1, model id `qwen3.5-4b`

## Invariants
- `--alias`, `baseURL`, and the `opencode.json` model key must all agree on `qwen3.5-4b`.
- Context is `50000` in the compose command and `opencode.json`; change together.
- `--jinja` is required — tool-calling breaks without it.
- `RAG_EMBEDDING_ENGINE=openai` is deliberate: prevents a ~1 GB local embedding model loading
  into 4 GB of swapless RAM. Cost is that document-chat/RAG is not usefully functional.

## Not yet done
Open WebUI signup is still open (`ENABLE_SIGNUP=false` not applied). Do that before
exposing publicly — see the open items in `integration-plan.md`.
