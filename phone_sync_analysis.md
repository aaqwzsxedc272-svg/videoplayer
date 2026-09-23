# Phone Sync Architecture Analysis

## Current Architecture (WiFi-Only)

The phone sync feature has **two parallel systems** — only one is actually active:

### 1. 🟢 Active: `PhoneFTPManager` (Lines 4003–4341)
This is the system actually used. It requires **both devices on the same WiFi** because:
- Connects directly to the phone's **local IP address** (e.g. `192.168.1.3`)
- Uses **standard FTP protocol** over the local network
- The phone runs an FTP server app (Solid Explorer, etc.)

**Data flow:**
```
PC ──(FTP connect to 192.168.x.x)──► Phone (Solid Explorer FTP Server)
PC ◄──(RETR command, binary stream)── Phone
```

**Key methods:**
- `PhoneFTPManager.connect_ftp()` — opens TCP to phone's local IP
- `PhoneFTPManager._crawl_directory()` — walks FTP tree via MLSD/LIST
- `PhoneFTPManager.run()` — worker loop that processes CRAWL/DOWNLOAD/DOWNLOAD_PERM ops
- `request_phone_file_sync()` (line 9445) — blocking call used by `BatchPhoneImageLoader`

### 2. 🔴 Legacy/Unused: `PhoneSyncServer` + `PhoneSyncHandler` (Lines 3559–3936)
An older HTTP+SSE system where the phone's browser pushes files to the PC.
- PC runs an HTTP server on port 8080
- Phone opens a web portal and uploads files via `POST /stream_upload`
- **This system is no longer wired in** — the app initializes `PhoneFTPManager`, not `PhoneSyncServer`

---

## The WiFi-Only Constraint — Root Causes

### Cause 1: Hard-coded local IP resolution
```python
# FTPConnectionDialog, line 4656-4657
self.ip_input = QLineEdit(last_ip)
self.ip_input.setPlaceholderText("e.g. 192.168.1.5")
```
The dialog only accepts a raw IP address. This IP must be reachable from the PC, which requires same-network connectivity.

### Cause 2: Direct TCP FTP connection
```python
# PhoneFTPManager.connect_ftp(), line 4052-4053
self.ftp = FTP()
self.ftp.connect(self.ip, self.port, timeout=5)
```
Standard `ftplib.FTP` opens a plain TCP socket to the given IP. If the phone is on a different network (e.g., mobile data), this TCP connection will fail immediately.

### Cause 3: FTP passive mode issues across NAT
FTP's PASV mode requires the server to open a second TCP channel back to the client, which doesn't work through NAT/firewalls without port forwarding.

---

## Approaches to Make It Work Without WiFi

### Option A: Tunnel via Ngrok / Cloudflare Tunnel (Easiest)
- Phone runs Solid Explorer FTP → user exposes it via ngrok/cloudflare tunnel
- PC gets a public hostname/port to connect to
- **Pro:** Zero code changes on the PC side
- **Con:** Requires manual setup on phone; ngrok has bandwidth limits

### Option B: Replace FTP with SFTP (SSH)
- Use `paramiko` library instead of `ftplib`
- Can tunnel over any SSH connection (e.g., a VPS)
- **Pro:** Encrypted, works through NAT
- **Con:** Phone needs an SSH/SFTP server app; more complex setup

### Option C: HTTP-based Pull (Revive the Legacy System + Tunnel)
Reverse the roles: **PC becomes the server**, phone's browser pushes files via HTTP POST.
1. PC's `PhoneSyncServer` (already coded, lines 3559–3936) runs on port 8080
2. Expose it via `ngrok` or `cloudflare tunnel` → phone gets a public URL
3. Phone opens that URL in browser → connects via SSE → streams files on demand

**This is essentially already built** — just needs a tunnel on the PC side and the `PhoneSyncServer` to be re-activated.

### Option D: Cloud Relay (Most Robust, Most Work)
- Both devices connect to a cloud relay (e.g., a VPS, or a service like Tailscale)
- **Tailscale** is the easiest: creates a virtual LAN across devices regardless of network
- With Tailscale, the FTP connection just uses the Tailscale IP (100.x.x.x) — **zero code changes needed**

---

## No-Internet / No-Cable Path: Phone Hotspot + FTP

This is the best fit for the requirement "without internet and without cable".

It does **not** need mobile data or an external router. It only needs a local wireless link:

1. Turn on the phone hotspot.
2. Connect the PC to that hotspot.
3. Start the FTP server app on the phone.
4. In the PC app, open Phone Sync and click **Detect Hotspot IP**.
5. Connect normally.

Why this works:
- A phone hotspot creates a private LAN even when there is no internet.
- The phone is usually the default gateway on that LAN.
- The app can detect that gateway IP and use it as the FTP host.
- Existing `PhoneFTPManager` continues to work unchanged for file listing/streaming.

Limits:
- If the PC has no Wi-Fi adapter and there is no router/hotspot, wireless sync is impossible without a cable.
- If Android disables hotspot while mobile data is off on a specific device/ROM, use a local router with no internet instead.
- If the FTP app displays a different IP than the detected gateway, use the FTP app's shown IP.

---

## Recommended Path With Internet: Tailscale (Option D) — Zero Code Changes

1. Install Tailscale on both PC and phone
2. Both join the same Tailscale network (free plan supports up to 3 devices)
3. Phone runs Solid Explorer FTP as usual
4. In the PC app's FTP dialog, enter the phone's **Tailscale IP** (100.x.x.x) instead of local IP

This is the path of least resistance since the existing FTP infrastructure is solid.

---

## If You Want In-App Fix: Auto-Tunnel via Cloudflare/Ngrok

### What to change:
1. **Revive `PhoneSyncServer`** — it already handles HTTP push correctly
2. **Add tunnel auto-start** — when user clicks "Connect via Phone Sync", auto-start `cloudflared` or `ngrok`
3. **Show QR code** with the tunnel URL in the dialog so the user can open it on their phone

### Files to modify:
- Lines **3942–3992**: `PhoneSyncServer.run()` — add tunnel startup
- Lines **4696–4720**: `PhoneSyncDialog` — add QR code display + tunnel URL
- Lines **8761–8780**: `open_phone_sync()` — switch between FTP and HTTP modes

### Key new class: `TunnelManager`
```python
class TunnelManager:
    """Starts cloudflared/ngrok and returns a public URL for the local HTTP server."""
    def start(self, local_port: int) -> str: ...  # returns public https URL
    def stop(self): ...
```

---

## Current Connection Dialog (Lines 4619–4694)

```
FTPConnectionDialog
├── IP Address field (e.g. 192.168.1.3)  ← must be local/VPN IP
├── Port field (default 2121)
├── Username field
└── Password field
```

The dialog defaults (line 8766–8769) show:
- `last_ip = '192.168.1.3'`
- `last_port = 9999`
- `last_user = 'mou'`, `last_password = 'mou'`

---

## Summary of Key Line References

| Component | Lines | Purpose |
|-----------|-------|---------|
| `PhoneSyncHandler` | 3561–3935 | HTTP server handler (legacy, unused) |
| `PhoneSyncServer` | 3942–3992 | HTTP server thread (legacy, unused) |
| `PhoneFTPManager` | 4003–4341 | **Active** FTP client thread |
| `FTPConnectionDialog` | 4619–4694 | UI dialog for FTP credentials |
| `PhoneSyncDialog` | 4696–4720 | Legacy HTTP portal dialog (unused) |
| `phone sync init` | 6617–6651 | Startup: creates FTP manager, loads creds |
| `load_phone_library` | 8720–8728 | Load saved phone file tree from JSON |
| `open_phone_sync()` | 8761–8780 | Opens connection dialog |
| `request_phone_file_sync()` | 9445–9534 | Blocking file fetch (used by image loader) |
| `_phone_cache_path()` | 9403–9412 | Cache path for downloaded files |
| `_evict_phone_cache_if_needed()` | 9414–9443 | LRU cache eviction (500MB limit) |
