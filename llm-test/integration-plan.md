# Integration plan: fold the LLM host into the playbooks

Written **after** implementation, describing what was actually built on VM 100 (`testing`,
10.127.0.100) and what it takes to make it reproducible.

Nothing in this document has been applied. The running system was built by hand.

---

## 1. As-built state

Working and verified across a full reboot cycle (host and guest).

### Host (pavo) — **no persistent changes**
This is the headline finding. GPU passthrough currently requires **zero** host configuration:

- IOMMU (VT-d) is on by default on this kernel — no `intel_iommu=on` needed.
- The 3070 Ti sits alone in **IOMMU group 17** with its audio function — no ACS override.
- **Proxmox binds `hostpci` devices to `vfio-pci` automatically at VM start.** No
  `/etc/modprobe.d/vfio.conf`, no nouveau blacklist, no `initcall_blacklist=sysfb_init`.
- This works because **the Intel iGPU (`00:02.0`) is enabled in BIOS** and drives the host
  console, so nothing contends for the NVIDIA framebuffer.

> The original plan called for blacklist files, vfio binding, and three kernel flags. All of it
> turned out to be unnecessary. **The one prerequisite is the iGPU being enabled in firmware** —
> that is a physical BIOS setting, not automatable, and must be documented as a host requirement.

### VM 100 config (Proxmox-side)
| Setting | Value |
|---|---|
| machine / bios | `q35` / `ovmf` + `efidisk0` (`efitype=4m,pre-enrolled-keys=0`) |
| hostpci0 | `0000:01:00,pcie=1` (both functions, no `x-vga`) |
| cores / memory | 4 / 4096 MB |
| disk | 40 GB on `local-lvm` |
| net0 | `virtio,bridge=vmbr1` @ 10.127.0.100 |

Switching seabios→OVMF worked in place because the Debian genericcloud image ships both a BIOS
boot partition and a populated ESP (incl. the `/EFI/BOOT/BOOTX64.EFI` fallback).

### Guest software
| Component | Version / value |
|---|---|
| OS / kernel | Debian 13, `6.12.101+deb13-cloud-amd64` |
| NVIDIA driver | `610.57.04` (DKMS, from NVIDIA `debian13` repo), CUDA UMD 13.3 |
| Docker / Compose | `29.7.2` / `5.5.0` |
| nvidia-container-toolkit | `1.20.0-1` |
| llama.cpp image | `ghcr.io/ggml-org/llama.cpp:server-cuda` `sha256:750f5682…` |
| Open WebUI image | `ghcr.io/open-webui/open-webui:main` `sha256:6a773e5c…` |
| Model | `Qwen_Qwen3.5-4B-Q4_K_M.gguf`, 3 013 027 808 B |
| Model sha256 | `13c16f426047e2de38cd075bdade4a7bcbc8c774384876f677740cda65f8a983` |

### Verified behaviour
- Full GPU offload. Effective context **50176** — llama.cpp rounds `-c 50000` up to a 256-multiple.
- **Tool-calling works** in both thinking and non-thinking modes (`finish_reason: tool_calls`,
  well-formed arguments). Qwen3.5 is a reasoning model: it returns a separate `reasoning_content`
  field, which Open WebUI renders as a collapsible section. `chat_template_kwargs:
  {"enable_thinking": false}` suppresses it per request; OpenCode needs no such setting.
- **Survives reboot unattended** — verified on both host and guest. The GPU re-attaches and both
  containers auto-start via `restart: unless-stopped`.
- Reachable from the laptop over netbird on both ports.

Resource numbers are in §2.

---

## 2. Sizing reference

Measured on the running system. These are the numbers that constrain the configuration.

| Resource | Measured | Implication |
|---|---|---|
| `llama.cpp:server-cuda` image | 6.98 GB | Container images dominate disk use, not the model |
| `open-webui:main` image | 7.16 GB | |
| Model file | 3.01 GB | |
| **Disk in use** | **22 GB of 40 GB** | **40 GB is a floor, not headroom — the two images alone exceed a 16 GB disk** |
| VRAM @ 50k context | 4823 MiB of 8192 | ~2.6 GB spare; context is the cheapest upgrade available (§5) |
| Host RAM | 2555 MB of 3854 | ~1.3 GB free with **no swap** — an overshoot is an OOM kill, not a slowdown |
| — llama-server (host side) | 1.19 GB | Weights live in VRAM; this is CUDA host context + runtime |
| — Open WebUI | 1.01 GB | With the local embedding model disabled |
| Generation | ~128 tok/s | |
| Prompt eval | 100–240 tok/s | A full 50k-token prompt costs minutes on a cold cache |

Host-side GPU configuration is **zero bytes** — see §1.

---

## 3. What to build in Ansible

### 3.1 Inventory (`inventories/prod/hosts.yml`)
```yaml
    llm_vm01:
      ansible_host: 10.127.0.100
      vm_id: 100
      vm_storage_gb: 40
      vm_cores: 4
      vm_memory_mb: 4096
```
```yaml
    llm_vm:
      hosts:
        llm_vm01:
      vars:
        llm_model_repo: bartowski/Qwen_Qwen3.5-4B-GGUF
        llm_model_file: Qwen_Qwen3.5-4B-Q4_K_M.gguf
        llm_model_sha256: 13c16f426047e2de38cd075bdade4a7bcbc8c774384876f677740cda65f8a983
        llm_context: 50000
        llm_api_port: 8081
        llm_ui_port: 8080
```
Add `llm_vm` under `generic_deb_vms.children`. The existing *provision debian cloud vms* play
already handles creation — but see §3.2, it cannot yet express this VM.

### 3.2 Extend `provision debian cloud vms` (`bootstrap.yml:670`)
The play currently hardcodes seabios/i440fx and has no GPU support. Needs optional, defaulted:
- `machine`, `bios`, `efidisk0` — required for passthrough
- `hostpci` — `community.proxmox.proxmox_kvm` supports it
- per-host `vm_cores` / `vm_memory_mb` (already vars, just need the group overrides)

Keep the defaults exactly as today so VMs 252/253 render byte-identical; gate the new keys on
`when:` / `default(omit)` so only `llm_vm01` gets them.

### 3.3 New role: `llm_host`
Ordered tasks, all verified by hand:

1. **Kernel headers — `linux-headers-cloud-amd64`, NOT `linux-headers-amd64`.**
   The cloud image runs the `-cloud-` kernel flavour; the generic meta-package installs
   non-matching headers and the DKMS build fails. This cost a debug cycle.
2. NVIDIA repo: `cuda-keyring_1.1-1_all.deb` from `…/repos/debian13/x86_64/`, then `cuda-drivers`.
   Long DKMS build — set a generous `async`/timeout.
3. Docker CE via `deb822_repository` — **near-duplicate of the `netbird_vm` play**; factor both
   into a shared `docker` role rather than copying a third time.
4. `nvidia-container-toolkit` + `nvidia-ctk runtime configure --runtime=docker`, restart docker.
5. `get_url` the GGUF to `/srv/llm/models/` with `checksum: sha256:{{ llm_model_sha256 }}`.
6. Template `docker-compose.yml` → `/srv/llm/`, then `community.docker.docker_compose_v2`.

**Pin both image digests.** `:main` and `:server-cuda` are moving tags; an unpinned re-run can
silently change the runtime.

### 3.4 Exposure (public)
1. `proxy_routes` += `{ host: "{{ llm_domain }}", backend: "…:{{ llm_ui_port }}" }`.
   `Caddyfile.j2` needs **no change** — its `{% else %}` branch already emits a plain
   `reverse_proxy`.
2. `llm_domain` into `secrets.yml`.
3. **Generalize the Unbound override** (`bootstrap.yml:557`) to `loop: "{{ proxy_routes }}"` —
   it is hardcoded to netbird and is the blocker for any second proxied host. Detail in §5.
4. Public DNS A record → ***REMOVED*** (manual, outside the repo).

---

## 4. Hard-won gotchas to encode

**4.1 Any task writing initramfs-bound files must notify an initramfs rebuild.**
`.link` files, `/etc/udev/rules.d/`, `/etc/modprobe.d/`, `/etc/crypttab`. Drift between `/etc` and
the initramfs took the whole homelab offline mid-build: Ansible wrote NIC `.link` files without
regenerating the initramfs, so the host only booted correctly *because* the initramfs was stale.
The first rebuild — mine, but any kernel upgrade would have done it — exposed a name-swap collision
(`Failed to rename … 'eth0' to 'eth1': File exists`) and bridged both vmbrs to the wrong NICs.
Already fixed by renaming to `wan0`/`lan0`, outside the `eth*` namespace the kernel assigns.
Pair the handler with `meta: flush_handlers` before any reboot task, and a post-reboot
`assert` on MAC↔name mapping — the failure was **silent**.

**4.2 The iGPU is a documented prerequisite, not an optimisation.**
Without it the NVIDIA card is the console GPU, and taking it away leaves a headless host with no
recovery path. Note it in the README; it can't be automated.

**4.3 Don't reflexively add `iommu=pt` / vfio config.** It wasn't needed and was implicated in the
outage. Add host tuning only against a measured problem.

---

## 5. Increasing context in the future

There is **~2.6 GB of VRAM headroom** (4823 MiB of 8192 used), so context is the cheapest upgrade
available. Change `-c` in the compose command and `limit.context` in `opencode.json` together.

### How to read the current numbers

Everything below is live on the running system — no instrumentation to add.

**VRAM — the budget that decides context.**
```bash
ssh root@10.127.0.100 'nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv'
# live view while generating:
ssh root@10.127.0.100 'watch -n1 nvidia-smi'
```
`memory.used` is the whole picture: weights + KV cache + CUDA context. Compare against 8192 MiB.
Read it **while a long generation runs**, not at idle — KV grows with the conversation.

**Context actually in effect** (llama.cpp rounds `-c` up to a multiple of 256):
```bash
curl -s http://10.127.0.100:8081/props | python3 -m json.tool | grep n_ctx
curl -s http://10.127.0.100:8081/slots | python3 -m json.tool | head -30
```
`/slots` shows all 4 slots and per-slot occupancy — the direct way to see concurrency eating
into the shared context budget.

**Throughput and request counters:**
```bash
curl -s http://10.127.0.100:8081/metrics | grep -E "^llamacpp:"
```
Prometheus format (`--metrics` is already enabled). Useful ones: `requests_processing`,
`requests_deferred` (>0 means you are out of slots), `prompt_tokens_seconds`.

Per-request timings, the most useful day-to-day signal:
```bash
ssh root@10.127.0.100 'docker logs --tail 50 llama-server 2>&1 | grep print_timing'
```
Each line gives prompt-eval and generation tok/s for one request.

**Host RAM — the tighter limit, and the one with no safety net:**
```bash
ssh root@10.127.0.100 'free -m; docker stats --no-stream --format "{{.Name}}: {{.MemUsage}}"'
```
Watch the `available` column in `free -m`. There is no swap, so if it approaches zero the kernel
kills a container outright. `docker stats` attributes it per service.

**Disk:**
```bash
ssh root@10.127.0.100 'df -h /; du -sh /srv/llm/models; docker system df'
```
`docker system df` matters most — images are the bulk, and old ones survive an image-tag bump.

**A note on the startup log:** this build does not print a per-buffer KV/compute breakdown, so
`nvidia-smi` is the authority on VRAM. `docker logs llama-server | head -30` still confirms the
model path, `n_slots`, `n_ctx_slot`, and `kv_unified`.

**Why this model is unusually cheap at long context.** Qwen3.5-4B is 32 layers = **24 Gated-DeltaNet
+ 8 full-attention**. Only the 8 full-attention layers hold a growing KV cache; the DeltaNet layers
keep a fixed-size recurrent state (~50 MiB total) no matter the context length.

- KV/token = `8 layers x 4 kv-heads x 256 head_dim x 2 (K+V) x 2 B (F16)` = **32 KiB/token**
- A conventional 4B would be ~144 KiB/token — roughly **4.5x** more.

| Context | KV cache | Projected total | Fits in 8 GB? |
|---|---|---|---|
| 50 000 *(current)* | 1.64 GB | **4.7 GB measured** | yes, 2.6 GB spare |
| 65 536 | 2.15 GB | ~5.3 GB | yes, comfortable |
| 98 304 | 3.22 GB | ~6.4 GB | yes, ~1.6 GB margin |
| 131 072 | 4.29 GB | ~7.4 GB | **tight — likely OOM under load** |
| 262 144 (native max) | 8.59 GB | ~11.7 GB | no |

**Recommendation: 98 304 is the practical ceiling** at F16 KV. Stop there unless you measure.

**Ways to go further, in order of preference:**
1. **Quantised KV cache** — `--cache-type-k q8_0 --cache-type-v q8_0` roughly halves KV, putting
   131k at ~5.3 GB and making ~196k viable. Small quality cost, by far the best lever here.
2. **A smaller quant** — IQ4_XS is 2.67 GB vs Q4_K_M's 3.01 GB, buying ~0.34 GB for real quality
   loss. Poor trade; prefer option 1.
3. **More VRAM** — the only route to the native 262k.

**Constraints that do *not* move with VRAM:**
- **`--parallel`/slots.** Currently 4 slots with `kv_unified=true`. Concurrent requests share the
  budget; long context and concurrency trade off directly.
- **Host RAM, not VRAM, is the tighter limit.** ~1.3 GB free, no swap. Context lives in VRAM so it
  barely moves this — but it's why `RAG_EMBEDDING_ENGINE=openai` is set (stops Open WebUI loading
  a ~1 GB local embedding model, at the cost of working document-chat). **Raise VM memory to
  8192 MB before re-enabling RAG.** pavo has ~20 GB free.
- **Prompt processing is not free.** ~100–240 tok/s, so a full 50k-token prompt costs minutes on a
  cold cache. Large contexts are far more useful with prompt caching than for single-shot fills.

---

## 5b. Client config refresh

`opencode.json` in this directory is the source of truth; `~/.config/opencode/` holds a **copy**.
Changing context, `baseURL`, or the model id here does nothing until you re-copy:

```bash
cp llm-test/opencode.json ~/.config/opencode/opencode.json
```

Do this after every edit to `opencode.json` — nothing syncs it, and a stale copy fails silently
(OpenCode keeps budgeting against the old `context` value). Current values: `context` 100000,
`output` 8192, against a server `n_ctx` of 200192.

Both numbers are **client-side declarations only**. llama-server runs `-n -1` (unlimited output)
and does not enforce or even see them; `context` tells OpenCode when to compact its own history,
and `output` is the headroom it reserves for a reply (usable input ≈ `context - output`).

---

## 6. Open items
- [ ] **Open WebUI hardening not yet applied** — `ENABLE_SIGNUP=false`, `DEFAULT_USER_ROLE=pending`.
      Do this *before* public DNS exists.
- [ ] Consider Caddy `basic_auth` on the LLM vhost. Needs `Caddyfile.j2` to make the directive
      conditional per-route.
- [ ] **llama-server (:8081) has no API key and permissive CORS.** It logs this at startup:
      `CORS is set to allow all origins ('*') and no API key is set`. The port is not proxied
      publicly — only Open WebUI's 8080 goes through Caddy — but it is reachable unauthenticated
      from anywhere on the netbird mesh. `--api-key` plus a matching `apiKey` in `opencode.json`
      and `OPENAI_API_KEY` in the compose file closes it.
- [ ] **No rate limiting anywhere.** 4 GB, no swap, one GPU — a handful of concurrent generations
      will OOM a container. A chat UI tolerates public exposure far worse than netbird's control plane.
- [ ] Model choice: 4B is small for agentic coding. Tool-calling is mechanically correct, but
      multi-step reliability is the open question. 8 GB VRAM caps you near a 14B at Q4.
- [ ] Vision unused — the model is multimodal; `mmproj-…-f16.gguf` (0.67 GB) would enable images.
