# Orbi Tunnel — make your home Orbi reachable from your phone

## What this does, in plain English

Your Orbi runs on your home computer at `http://localhost:5050`. That address
only works when you're sitting in front of that computer. If you want to use
the Orbi PWA on your phone while you're out shopping or visiting a customer,
your phone has no way to reach it.

A **Cloudflare tunnel** fixes that. It opens a one-way outbound connection
from your home computer to Cloudflare's network, and Cloudflare gives you a
public web address (like `https://orbi.yourdomain.com`) that quietly forwards
traffic back down that outbound connection to your home Orbi.

Three things make this safe:

1. **No port forwarding.** You don't open any hole in your home router. The
   tunnel is outbound only — same direction your web browser already uses.
2. **HTTPS at Cloudflare's edge.** Every connection from your phone is
   encrypted by Cloudflare with a real, valid certificate. You don't have to
   set up SSL yourself.
3. **Cloudflare can't see your Orbi data in a useful way.** They route
   bytes; the application (Orbi) sits on your computer, the data sits on
   your computer, the AI sits on your computer. Cloudflare is a pipe.

Optional: you can add Cloudflare Access on top to require a one-time email
code before anyone can hit the URL. That's a one-checkbox upgrade in the
Cloudflare dashboard once the tunnel is running.

## What you need before starting

- A **free Cloudflare account** — sign up at https://dash.cloudflare.com
  (no credit card required)
- A **domain name added to Cloudflare**. A brand-new $10/year domain works
  fine. You add it to Cloudflare by changing the domain's nameservers (the
  Cloudflare dashboard walks you through it)
- A computer running Orbi, where Orbi is reachable at `http://localhost:5050`

## The easy way: run the script

```bash
cd customer_install/tunnel
chmod +x setup_cloudflared.sh
./setup_cloudflared.sh
```

The script will:

1. Detect your operating system (Mac / Linux / Windows-WSL)
2. Tell you how to install the `cloudflared` program if you don't have it
3. Log you in to Cloudflare (opens a browser tab)
4. Create a tunnel named `orbi-<random suffix>`
5. Write a config file at `~/.cloudflared/config.yml`
6. Create the DNS record so your chosen hostname points at the tunnel
7. Tell you the single command to run the tunnel

Every step asks for your okay before doing anything.

## The manual way (if the script doesn't work)

### 1. Install cloudflared

| OS | Command |
|----|---------|
| macOS | `brew install cloudflared` |
| Linux (Intel/AMD) | Download `cloudflared-linux-amd64.deb` from the [releases page](https://github.com/cloudflare/cloudflared/releases/latest), then `sudo dpkg -i cloudflared-linux-amd64.deb` |
| Linux (ARM/Raspberry Pi) | Same as above but use the `arm64` `.deb` |
| Windows | Download `cloudflared-windows-amd64.exe` from the releases page |
| Windows + WSL | Use the Linux `amd64` instructions inside WSL |

### 2. Log in to Cloudflare

```bash
cloudflared tunnel login
```

Your browser opens, you sign in to Cloudflare and pick the domain you want
Orbi to use. Cloudflare writes a certificate to `~/.cloudflared/cert.pem`.

### 3. Create a tunnel

```bash
cloudflared tunnel create orbi-home
```

(You can use any name — `orbi-home` is just an example.)

Note the **tunnel UUID** that gets printed. It looks like
`a3f10b00-1234-4567-89ab-1122334455ff`.

### 4. Write the config file

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <YOUR-TUNNEL-UUID-FROM-STEP-3>
credentials-file: /home/YOUR-USER/.cloudflared/<YOUR-TUNNEL-UUID>.json

ingress:
  - hostname: orbi.yourdomain.com
    service: http://localhost:5050
  - service: http_status:404
```

Replace `orbi.yourdomain.com` with the hostname you actually want.

### 5. Add the DNS record

```bash
cloudflared tunnel route dns orbi-home orbi.yourdomain.com
```

This creates a CNAME in your Cloudflare DNS pointing the chosen hostname at
the tunnel. (You can also do it by hand in the Cloudflare dashboard — point
the CNAME at `<YOUR-TUNNEL-UUID>.cfargotunnel.com`.)

### 6. Run the tunnel

```bash
cloudflared tunnel run orbi-home
```

Leave that command running. Open `https://orbi.yourdomain.com` on your
phone — you should see your Orbi login.

### 7. (Optional) Run it on boot

```bash
sudo cloudflared service install
```

That installs cloudflared as a system service so the tunnel comes up
automatically every time your computer starts.

## Troubleshooting

**"cloudflared: command not found"**
You didn't finish the install step. Re-run step 1.

**"Cannot determine default origin certificate path"**
You haven't logged in yet. Run `cloudflared tunnel login`.

**Browser shows "522 Connection timed out"**
Your Orbi isn't actually running on `localhost:5050`. In another terminal,
check with `curl http://localhost:5050` — if that fails, start Orbi first.

**Browser shows "1033 Argo Tunnel error"**
The `cloudflared tunnel run` command isn't running, or it crashed. Restart
it and watch its output for the real error.

**Hostname doesn't resolve at all**
The DNS route step didn't take. Open the Cloudflare dashboard → your domain
→ DNS, and add a CNAME by hand pointing the chosen hostname at
`<TUNNEL-UUID>.cfargotunnel.com` (proxied — orange cloud on).

**Want to lock it down even more?**
In the Cloudflare dashboard, go to Zero Trust → Access → Applications, add
your tunnel hostname as an application, and require a one-time PIN sent to
your email. Cloudflare will block anyone but you, before traffic ever
reaches your home computer.

## Files this writes on your computer

| Path | What it is |
|------|------------|
| `~/.cloudflared/cert.pem` | Your Cloudflare login certificate |
| `~/.cloudflared/<UUID>.json` | The tunnel's credentials |
| `~/.cloudflared/config.yml` | The tunnel config (which hostname maps to which local port) |
| `~/.cloudflared/config.yml.bak.YYYYMMDD…` | Backup of any previous config the script overwrote |

Nothing else on your system is touched.
