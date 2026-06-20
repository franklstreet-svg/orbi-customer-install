# Orbi Cross-Platform Installers

What gets emailed/handed to customers so they can install Orbi on their own
Windows or Mac PC (or Linux box) with a double-click.

## Layout

```
cross_platform/
├── windows/                 ← Windows-specific install + uninstall scripts
│   ├── Install Orbi.bat
│   └── Uninstall Orbi.bat
├── mac/                     ← Mac-specific install + uninstall scripts
│   ├── Install Orbi.command
│   └── Uninstall Orbi.command
├── shared/                  ← Cross-platform helpers
│   └── install_helper.py
├── bin/                     ← Platform binaries (downloaded separately)
│   ├── windows/
│   │   ├── nssm.exe         ← Windows service manager
│   │   ├── cloudflared.exe  ← Cloudflare tunnel
│   │   └── llama-server.exe ← (optional) local LLM
│   └── mac/
│       ├── cloudflared-mac
│       └── llama-server-mac (optional)
├── build_packages.sh        ← Builds the downloadable ZIPs
└── dist/                    ← Output — what customers download
    ├── orbi-windows.zip
    └── orbi-mac.zip
```

## Customer experience

### Windows customer:
1. Downloads `orbi-windows.zip` from a link Frank gives them
2. Right-click → Extract All
3. Right-click `Install Orbi.bat` → Run as administrator
4. Fills in business name, owner email + password, Frank-provided brain URL + API key
5. Installer:
   - Verifies/installs Python via `winget`
   - Copies files to `C:\Program Files\Orbi\`
   - Bundles in local LLM (3B model) for offline tier
   - Registers Orbi as a Windows service (auto-starts on boot)
   - Creates desktop shortcut that opens Orbi in app-mode
   - Opens owner dashboard in their default browser
6. Customer is live. Their data lives at `C:\Program Files\Orbi\data\`.

### Mac customer:
1. Downloads `orbi-mac.zip`
2. Double-clicks to unzip (Mac auto-extracts)
3. Double-clicks `Install Orbi.command` (may need to right-click → Open first time)
4. Fills in same details
5. Installer:
   - Verifies/installs Python via Homebrew
   - Copies files to `~/.orbi/`
   - Registers as a LaunchAgent (auto-starts when user logs in)
   - Creates `Open Orbi.command` shortcut on Desktop
   - Opens owner dashboard
6. Customer is live. Their data lives at `~/.orbi/data/`.

### Linux customer:
Existing `customer_install/install.sh` — no change.

## Building the packages

```bash
bash /home/frank/orbi_web/cross_platform/build_packages.sh
```

Output goes to `dist/orbi-windows.zip` and `dist/orbi-mac.zip`. About 1.9GB each
(local 3B LLM is most of it — can drop to ~50MB by removing it and relying on
the brain + HuggingFace).

## Platform binaries

Currently bundled (downloaded automatically by Frank earlier):
- `bin/windows/nssm.exe` ← 330KB
- `bin/windows/cloudflared.exe` ← 54MB
- `bin/mac/cloudflared-mac` ← 41MB

Still missing (optional, only needed for offline LLM tier):
- `bin/windows/llama-server.exe` — grab from https://github.com/ggerganov/llama.cpp/releases
- `bin/mac/llama-server-mac` — same

Without these, the local LLM tier is disabled and customer relies on
the brain machine + HuggingFace failover. That's fine — most customers
will never be fully offline.

## Distribution

The two ZIPs need to be served from somewhere reachable:
- Frank's own server: `https://orbi.frank.com/download/`
- S3 / R2 bucket with public read
- GitHub Releases (if open-sourcing)
- Direct email attachment (too big at 1.9GB — won't work)
- USB drive (handy for in-person installs)

## Limitations of this v1

- **Not code-signed.** Windows will show "Unknown publisher" warning. Mac will
  require right-click → Open the first time. Customer just has to click through.
  Signing planned for v1.1 (~$200/yr Windows cert + $99/yr Apple Developer).
- **Customer's PC must be on** for Orbi to take phone calls. If they shut
  down their PC at night, calls won't be answered. Same caveat as any
  desktop-installed business software.
- **Customer needs admin rights** to install on Windows. Most small business
  owners do.
- **Customer needs internet** for first install (Python download). After that,
  Orbi runs offline if the brain is unreachable.

## Updating

When you push a new Orbi version, customers can either:
1. Re-run `Install Orbi.bat` / `Install Orbi.command` on top of the existing
   install (it overwrites files but keeps their data).
2. Or trigger an in-app update (Phase 2 feature — not yet built).
