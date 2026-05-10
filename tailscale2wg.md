# Plan — Migrate Whisper API from Tailscale sidecar to WireGuard sidecar

Branch: continue on `refactor/wireguard` (already checked out).

## Context

Current state (`refactor/dockerize` merged): whisper-api runs in Docker on the user's Windows PC (GPU host) behind a Tailscale sidecar (`tailscale/tailscale`). API is reachable only by tailnet peers, X-API-Key on top.

New topology requested:

- **VPS** runs the WireGuard **server**, the frontend, and other services.
- **PC** (this host, GPU) runs as a WireGuard **client** that dials the VPS.
- **Other user devices** (laptop, phone) are also WG clients on the same tunnel.
- Frontend on VPS calls whisper-api over WG using PC's WG IP.

Goal: replace the Tailscale sidecar with a WireGuard **client** sidecar. whisper-api keeps sharing the sidecar's network namespace so the only path to the API is the WG tunnel. Drop all Tailscale code, env, docs.

User decisions (locked):

1. WG client runs in a **sidecar container** (not native Windows WG client). Mirrors current Tailscale shape, avoids Docker-Desktop-on-Windows host-network pain.
2. Keep **X-API-Key** layer on top of WG (defense in depth).
3. **Drop `ALLOWED_IPS`** entirely from `security.py` and `.env.example`. WG peer membership is the network gate.

## Approach

Two services in `docker-compose.yml`:

- `wireguard` — `linuxserver/wireguard` in **client mode**. Reads `wg0.conf` (provided by user from VPS server) from a bind-mounted config dir. Brings up the tunnel on container start. Owns the network namespace and port 8000 inside the WG tunnel.
- `whisper-api` — unchanged build; uses `network_mode: "service:wireguard"`. No `ports:` block. GPU reservation kept.

Auth layers:
1. WG tunnel — only configured peers reach the socket.
2. `X-API-Key` header — validated by `security.py`.

## Files to change

- `docker-compose.yml` — replace `tailscale` service with `wireguard` service; rename `network_mode` target; rename volume; remove `tailscale-state` volume.
- `.env.example` — remove `TS_AUTHKEY`, `TS_HOSTNAME`; remove deprecated `ALLOWED_IPS`; keep `API_KEY`; document `wg0.conf` location.
- `security.py:23-32` — delete the `ALLOWED_IPS` block. Keep `X-API-Key` block. Keep `OPTIONS` and `PUBLIC_PATHS` short-circuits.
- `README.md` — replace Tailscale instructions with WireGuard client config + bring-up steps.
- `CLAUDE.md` — update API access section: "exposed only through WireGuard tunnel to VPS" instead of Tailscale.
- `refactor-dockerize.md` — leave as-is (historical record). Optionally add a one-line note pointing to `tailscale2wg.md` as the successor plan.
- New file: `wireguard/wg0.conf.example` (committed) — template for the WG client config. Real `wireguard/wg0.conf` git-ignored.
- `.gitignore` — add `wireguard/wg0.conf`.

Files NOT changed: `transcriber.py`, `audio_to_text_file.py`, `api.py`, `Dockerfile`, `requirements.txt`. CLI mode keeps working unchanged.

## Reference: docker-compose.yml shape

```yaml
services:
  wireguard:
    image: lscr.io/linuxserver/wireguard:latest
    container_name: whisper-wireguard
    cap_add:
      - NET_ADMIN
      - SYS_MODULE
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
    volumes:
      - ./wireguard:/config/wg_confs   # contains wg0.conf (client config from VPS)
      - /lib/modules:/lib/modules:ro
    sysctls:
      - net.ipv4.conf.all.src_valid_mark=1
    restart: unless-stopped

  whisper-api:
    build: .
    container_name: whisper-api
    network_mode: "service:wireguard"
    depends_on:
      - wireguard
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
```

Notes:
- `linuxserver/wireguard` runs in **client mode** automatically when `PEERS` env is not set and `wg_confs/wg0.conf` exists. The image's startup script does `wg-quick up wg0` for each `*.conf` it finds.
- `network_mode: "service:wireguard"` requires whisper-api to have **no** `ports:` block. Compose enforces.
- Healthcheck stays valid — shared netns means `localhost:8000` resolves to the wireguard container's interfaces, including the API socket bound by uvicorn.
- GPU reservation untouched. Only network namespace is shared, not device namespace.
- `/lib/modules` read-only mount lets the container load the kernel `wireguard` module on hosts that support it. On Docker Desktop / WSL2 the module is usually already present; if not, fallback is `boringtun` userspace (slower) — set `USE_BORINGTUN=true` in `wireguard` env.

## Reference: wireguard/wg0.conf.example

```ini
[Interface]
# PC's WG client identity. Replace with values issued by VPS WG server.
PrivateKey = <PC_PRIVATE_KEY>
Address    = 10.0.0.5/32          # PC's IP inside the WG network
DNS        = 1.1.1.1              # optional; remove if VPS handles DNS

[Peer]
# VPS WG server.
PublicKey            = <VPS_PUBLIC_KEY>
Endpoint             = vps.example.com:51820
AllowedIPs           = 10.0.0.0/24    # routes ONLY WG subnet through tunnel
PersistentKeepalive  = 25
```

`AllowedIPs = 10.0.0.0/24` is intentional — we only want WG-network traffic through the tunnel, not all internet. If the VPS frontend lives in this subnet, it reaches whisper-api via `http://10.0.0.5:8000`.

The real `wireguard/wg0.conf` is git-ignored. User generates it from the VPS WG admin (e.g. wg-easy / `wg genkey`).

## Reference: .env.example shape

```env
# API key required (defense in depth on top of WG tunnel)
API_KEY=change-me-to-a-random-secret

# Optional: control where whisper caches the model
# XDG_CACHE_HOME=/cache
```

Removed: `TS_AUTHKEY`, `TS_HOSTNAME`, `ALLOWED_IPS`.

## security.py change

Delete lines 23-32 (the `ALLOWED_IPS` block). Resulting middleware does only OPTIONS pass-through, public-path skip, and X-API-Key check. No CIDR work — WG already enforces network membership.

## Verification

1. Generate WG client keypair on PC, register the public key as a peer on the VPS WG server, write `wireguard/wg0.conf` from that.
2. `docker compose up --build -d`
3. `docker compose logs wireguard` — expect `wg-quick: [#] ip link add wg0 type wireguard` and `[#] wg setconf …` without errors. Confirm `Interface for wg0 is up`.
4. From inside wireguard container: `docker compose exec wireguard wg show` — expect `latest handshake` populated within ~30s; expect peer = VPS public key.
5. `docker compose logs whisper-api` — expect "Loading Whisper model on startup..." and `Model loaded on device: cuda`.
6. From the **VPS** (which is on the WG network): `curl http://10.0.0.5:8000/health` — expect `{"status":"ok","device":"cuda",...}`.
7. From **another WG-peer device** (laptop): same `curl` — expect 200.
8. From a **non-WG device on LAN**: `curl http://<PC-LAN-ip>:8000/health` — expect connection refused / timeout. Confirms no LAN exposure.
9. From WG device, missing API key: `curl -X POST http://10.0.0.5:8000/transcribe -F file=@x.mp3` — expect `403 Invalid or missing API key`.
10. From WG device with key: full upload + `/jobs/{id}` poll cycle → `completed` with transcription.
11. GPU sanity: `/health` shows `"device":"cuda"`. If `cpu`, NVIDIA Container Toolkit / WSL2 GPU passthrough broken — fix before shipping.

## Risks / gotchas

- **WSL2 + WireGuard kernel module**: Docker Desktop's WSL2 distro ships with WG kernel module on recent versions. If `wg-quick up` fails with `Unable to find module 'wireguard'`, switch the image to userspace by adding `- USE_BORINGTUN=true` to the `wireguard` service env. Slight throughput hit, no other change.
- **Endpoint NAT**: PC behind home NAT — `PersistentKeepalive = 25` is required to keep the NAT mapping alive so the VPS can reach back. Already in the example config.
- **AllowedIPs scope**: keep `10.0.0.0/24` (or whatever the WG subnet is). Setting `0.0.0.0/0` would route ALL PC internet traffic through the VPS — not what we want, and would also break Docker pulls inside the wireguard container.
- **DNS inside whisper-api container**: shared netns means whisper-api also uses wireguard's resolver. If WG `DNS=` is set, that DNS handles all lookups (including `download.pytorch.org` during cold model fetches). Leave `DNS=` unset unless needed; the host's resolv.conf passes through.
- **Healthcheck path**: `urllib.request.urlopen('http://localhost:8000/health')` works because `localhost` inside whisper-api = the wireguard container's loopback = the bound uvicorn socket. Same as the Tailscale setup.
- **Killing the wireguard container kills whisper-api network**: `depends_on` doesn't restart whisper-api on wireguard restart. `restart: unless-stopped` handles container-level restart, but mid-flight requests will drop. Acceptable for single-user use.
- **Config secret hygiene**: `wireguard/wg0.conf` contains the PC's WG private key. Must be git-ignored. The `.example` template is safe to commit.
- **Frontend URL on VPS**: hardcoding `10.0.0.5` is fine for now; if you later want stable name, configure `wg0.conf` `DNS=` or set a hosts entry on VPS.

## Out of scope

- Per-IP rate limiting.
- Persisting jobs to disk.
- HTTPS termination — WG already encrypts; if frontend wants HTTPS to whisper-api, terminate at a reverse proxy on the VPS.
- CIDR support in `security.py` — not needed; WG gates network.
- Auto-rotating WG keys.
