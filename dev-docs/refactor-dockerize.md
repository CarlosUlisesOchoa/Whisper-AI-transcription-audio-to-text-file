# Plan — Tailscale-only Whisper Transcription API in Docker

Branch: `refactor/dockerize` (all commits go here)

## Context

Project goal: run the existing Whisper transcription FastAPI inside Docker on the user's main PC (Windows + Docker Desktop + WSL2), and **expose it ONLY to the user's Tailscale devices** — nothing on LAN, nothing public.

Current state of `refactor/dockerize` is mostly there: `Dockerfile`, `docker-compose.yml`, `api.py`, `security.py`, GPU reservation, healthcheck, cache volume — all wired. CLI mode (`audio_to_text_file.py`) untouched and still works.

Three real blockers prevent Tailscale-only access today:

1. **Source-IP NAT inside Docker bridge network** — container sees `172.x.x.x` (Docker gateway), not the real Tailscale `100.x.x.x` of caller. `ALLOWED_IPS` check in `security.py:24` therefore cannot identify the actual tailnet peer.
2. **`security.py` does exact-IP match only** — no CIDR. Tailnet uses `100.64.0.0/10`.
3. **`ports: "8000:8000"` binds 0.0.0.0** — `docker-compose.yml:6` exposes API on every host interface (LAN, public if forwarded).

User confirmed strategy:

- **Tailscale sidecar container** (`tailscale/tailscale`) — whisper-api shares its network namespace via `network_mode: "service:tailscale"`. API only reachable through tailnet.
- Host: Windows Docker Desktop (WSL2 backend).
- Auth: tailnet membership AND `X-API-Key` header (defense in depth).
- TS auth: reusable auth key in `.env` as `TS_AUTHKEY`.

## Approach

Run two services in `docker-compose.yml`:

- `tailscale` — sidecar that joins your tailnet on boot using `TS_AUTHKEY`. Holds the network namespace, owns port 8000 inside tailnet only.
- `whisper-api` — existing FastAPI; keeps GPU reservation; binds via `network_mode: "service:tailscale"`. Removes its own `ports:` block (port now belongs to sidecar).

Auth becomes 2 layers:
1. Layer 1: only tailnet peers can reach the socket at all (sidecar enforces).
2. Layer 2: `X-API-Key` header validated by `security.py`.

Drop the `ALLOWED_IPS` IP-whitelist path entirely (Tailscale ACLs are the right tool — configure them in the Tailscale admin console, not in app code). Leave the env var supported for backward compatibility but mark deprecated in `.env.example`. **No CIDR work needed in `security.py`** because tailnet membership already gates access.

## Files to change

- `docker-compose.yml` — add `tailscale` service, switch `whisper-api` to `network_mode: "service:tailscale"`, remove `ports:` from whisper-api, add `depends_on: tailscale` and add a named volume `tailscale-state` for `/var/lib/tailscale`.
- `.env.example` — add `TS_AUTHKEY=`, `TS_HOSTNAME=whisper-api`, mark `ALLOWED_IPS` deprecated.
- `README.md` — replace "Docker / API Mode" section: remove `localhost:8000` examples, replace with tailnet hostname (e.g. `http://whisper-api:8000` or MagicDNS name), document how to mint a Tailscale auth key, document Tailscale ACL recommendation.
- `CLAUDE.md` — update API section to reflect Tailscale-only access model.

Files NOT changed: `transcriber.py`, `audio_to_text_file.py`, `api.py`, `Dockerfile`. CLI mode keeps working unchanged.

## Reference: docker-compose.yml shape

```yaml
services:
  tailscale:
    image: tailscale/tailscale:latest
    container_name: whisper-tailscale
    hostname: ${TS_HOSTNAME:-whisper-api}
    environment:
      - TS_AUTHKEY=${TS_AUTHKEY}
      - TS_STATE_DIR=/var/lib/tailscale
      - TS_USERSPACE=false
      - TS_EXTRA_ARGS=--accept-dns=false
    volumes:
      - tailscale-state:/var/lib/tailscale
      - /dev/net/tun:/dev/net/tun
    cap_add:
      - net_admin
      - sys_module
    restart: unless-stopped

  whisper-api:
    build: .
    container_name: whisper-api
    network_mode: "service:tailscale"
    depends_on:
      - tailscale
    env_file:
      - .env
    environment:
      - XDG_CACHE_HOME=/cache
    volumes:
      - whisper-cache:/cache
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 120s

volumes:
  whisper-cache:
  tailscale-state:
```

Notes:
- `network_mode: "service:tailscale"` requires the whisper-api `ports:` block to be removed (mutually exclusive).
- `/dev/net/tun` mount + `net_admin` cap = required for kernel-mode TS. On Windows Docker Desktop (WSL2) the `/dev/net/tun` mount works inside the WSL2 VM. If it fails, fallback is `TS_USERSPACE=true` (slower but no kernel deps).
- GPU reservation still works because whisper-api still owns its own container; only network namespace is shared.

## Reference: .env.example additions

```env
# Tailscale sidecar
TS_AUTHKEY=tskey-auth-xxxxxxxxxxxx
TS_HOSTNAME=whisper-api

# API key still required (defense in depth)
API_KEY=change-me-to-a-random-secret

# DEPRECATED — tailnet ACLs are the source of truth now. Leave empty.
ALLOWED_IPS=
```

## Verification

1. Mint a reusable auth key at https://login.tailscale.com/admin/settings/keys — paste into `.env` as `TS_AUTHKEY`.
2. `docker compose up --build -d`
3. `docker compose logs tailscale` — confirm "Success." and node appears in Tailscale admin under hostname `whisper-api`.
4. `docker compose logs whisper-api` — confirm "Loading Whisper model on startup..." then `Model loaded on device: cuda`.
5. From a **second Tailscale device** (laptop, phone): `curl http://whisper-api:8000/health` (or the MagicDNS FQDN). Expect `{"status":"ok","device":"cuda",...}`.
6. From a **non-Tailscale device on the same LAN**: `curl http://<host-LAN-ip>:8000/health` → expect connection refused / timeout. Confirms no LAN exposure.
7. From Tailscale device, missing API key: `curl http://whisper-api:8000/transcribe -F file=@x.mp3` → expect `403 Invalid or missing API key`.
8. From Tailscale device with key: full upload + poll cycle → expect `completed` with transcription result.
9. GPU sanity: `/health` response must show `"device":"cuda"` and a GPU name. If `cpu`, NVIDIA Container Toolkit / WSL2 GPU passthrough is not configured — fix Docker Desktop GPU setup before shipping.

## Risks / gotchas

- **Windows Docker Desktop + `/dev/net/tun`**: kernel mode TS sometimes fails on Windows. If sidecar logs show tun errors, set `TS_USERSPACE=true`. Slight perf hit, otherwise transparent.
- **Auth key rotation**: reusable keys can be revoked from admin. Ephemeral keys cause node to disappear if container stops — not what user wants.
- **Tailscale ACLs**: by default tailnet allows all peers. If user shares the tailnet, lock the API down with an ACL rule restricting `whisper-api` to specific user devices.
- **MagicDNS**: ensure MagicDNS is enabled in the tailnet, otherwise call by raw `100.x.x.x` IP.
- **Healthcheck**: runs from inside the container against `localhost:8000`. Still works because in shared network namespace, localhost = tailscale container = the bound socket.
- **Removing `ports:` from whisper-api is mandatory** when using `network_mode: service:`. Compose will refuse otherwise.

## Out of scope (not blocking docker readiness)

- Per-IP rate limiting.
- Persisting jobs to disk (currently in-memory; OK for single-user tailnet).
- HTTPS — Tailscale already encrypts node-to-node via WireGuard. Optional Tailscale Serve/Funnel for TLS termination later.
- CIDR support in `security.py` — not needed once tailnet gates access. Skip.
