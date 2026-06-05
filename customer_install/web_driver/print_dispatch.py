"""
print_dispatch — generic order delivery via thermal receipt printer.

The same pattern DoorDash/UberEats/GrubHub use: format the order as a
thermal ticket, push it to the restaurant's existing receipt printer
over the network. The kitchen tears the ticket off the printer and
fulfills it like any other order.

Works with any **ESC/POS-compatible** thermal printer:
  · Epson TM-T20II / TM-T88 / TM-m30
  · Star TSP100 / TSP650
  · most other receipt printers that speak the ESC/POS protocol

Connection methods supported:
  · Network (TCP/IP — Epson default port 9100, most common)
  · USB (printer plugged directly into the customer's computer)
  · Serial (rare, older printers)

Network mode is the v1 focus — it covers the vast majority of in-
restaurant thermal printers. The printer's local IP goes in the
customer's profile under `order_submission.printer_ip` + optional
`printer_port` (default 9100).

Inputs/outputs match `web_driver.email_dispatch.submit_order` so the
dispatch layer can swap methods transparently.

If `python-escpos` isn't installed on the customer's machine, returns
a clear "missing_dep" error — Orby never silently drops an order.
"""

from __future__ import annotations

import logging
import os
import platform
import socket
import subprocess
import tempfile
import uuid

log = logging.getLogger("orbi.web_driver.print")


def _build_ticket_lines(order_id: str, cart: list[dict],
                         customer: dict, totals: dict,
                         profile: dict) -> list[str]:
    """Return the lines of the receipt as plain strings — kept simple so
    we can also dump it to a file or email it as a fallback if the
    printer is unreachable."""
    biz_name = profile.get("name", "")
    cust_name = (customer or {}).get("name", "Walk-in")
    cust_phone = (customer or {}).get("phone", "")
    pickup = (customer or {}).get("pickup_time") or "ASAP"
    lines = []
    if biz_name:
        lines.append(f"== {biz_name} ==")
    lines.append("    NEW ORDER (Orby)")
    lines.append("-" * 32)
    lines.append(f"Order: {order_id}")
    lines.append(f"Name:  {cust_name}")
    if cust_phone:
        lines.append(f"Phone: {cust_phone}")
    lines.append(f"Pickup: {pickup}")
    lines.append("-" * 32)
    for item in cart or []:
        qty = item.get("qty", 1)
        name = item.get("name", "(item)")
        price = item.get("price") or item.get("base_price") or 0
        line_total = float(price) * int(qty)
        lines.append(f"{qty}x {name[:24]:<24} ${line_total:>5.2f}")
        for mod in item.get("modifiers") or []:
            mod_name = mod if isinstance(mod, str) else \
                       (mod.get("label") or mod.get("name") or "")
            mod_price = 0 if isinstance(mod, str) else (mod.get("price_delta") or 0)
            if mod_name:
                tag = f"  + {mod_name[:22]}"
                if mod_price:
                    tag = f"{tag:<28} +${mod_price:.2f}"
                lines.append(tag)
    lines.append("-" * 32)
    lines.append(f"Subtotal:           ${totals.get('subtotal', 0):>6.2f}")
    if totals.get("tax", 0):
        lines.append(f"Tax:                ${totals.get('tax', 0):>6.2f}")
    lines.append(f"TOTAL:              ${totals.get('total', 0):>6.2f}")
    lines.append("")
    lines.append("Thanks - via Orby")
    lines.append("")
    return lines


def _send_via_escpos_network(printer_ip: str, printer_port: int,
                              lines: list[str]) -> tuple[bool, str]:
    """Send the ticket through python-escpos to a TCP-attached printer.
    Returns (ok, error_message)."""
    try:
        from escpos.printer import Network
    except ImportError:
        return False, "python-escpos not installed (pip install python-escpos)"

    try:
        printer = Network(printer_ip, port=printer_port, timeout=8)
        # Header in larger font
        try:
            printer.set(align="center", bold=True, double_height=True,
                         double_width=True)
        except Exception:
            pass
        for line in lines[:2]:
            printer.text(line + "\n")
        try:
            printer.set(align="left", bold=False, double_height=False,
                         double_width=False)
        except Exception:
            pass
        for line in lines[2:]:
            printer.text(line + "\n")
        printer.cut()
        printer.close()
        return True, ""
    except Exception as e:
        return False, f"escpos_send_failed: {e}"


def _send_via_raw_tcp(printer_ip: str, printer_port: int,
                       lines: list[str]) -> tuple[bool, str]:
    """Fallback when python-escpos isn't installed — raw TCP send of plain
    text + minimal ESC/POS escape codes. Crude but works for any printer
    that accepts plain-text over port 9100 (most do)."""
    INIT = b"\x1b\x40"          # Initialize printer
    CUT  = b"\x1d\x56\x00"      # Full cut
    LF   = b"\n"
    try:
        payload = INIT
        for line in lines:
            payload += line.encode("ascii", errors="replace") + LF
        payload += LF + LF + CUT
        with socket.create_connection((printer_ip, printer_port), timeout=8) as s:
            s.sendall(payload)
        return True, ""
    except Exception as e:
        return False, f"raw_tcp_failed: {e}"


def _send_via_system_printer(printer_name: str | None,
                               lines: list[str]) -> tuple[bool, str]:
    """Send the ticket through whatever printer is installed on the
    customer's computer at the OS level. Works for ANY printer the
    customer has set up — no IP address or USB ID needed.

    - Windows: PowerShell Out-Printer (uses the printer queue)
    - macOS / Linux: CUPS `lp` command
    - `printer_name` is the printer's name as it appears in the OS
      (e.g. "Star TSP100" or "Kitchen Printer"). Pass None to use
      the system default.

    Output is plain text — works on any printer driver. For better
    thermal formatting (font sizes, cut commands), the network or USB
    ESC/POS paths are preferred when available."""
    text = "\n".join(lines) + "\n"
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                           delete=False, encoding="utf-8") as f:
            f.write(text)
            tmp_path = f.name
    except Exception as e:
        return False, f"tempfile_failed: {e}"

    try:
        sys_name = platform.system().lower()
        if sys_name == "windows":
            # PowerShell Out-Printer: works with whatever the OS has registered
            # as a printer (USB, network, virtual PDF, etc.). Printer name is
            # optional — without it, the system default is used.
            ps_script = f"Get-Content -Path '{tmp_path}' | Out-Printer"
            if printer_name:
                # Escape single quotes in printer name for PowerShell
                safe_name = printer_name.replace("'", "''")
                ps_script += f" -Name '{safe_name}'"
            cmd = ["powershell", "-NoProfile", "-Command", ps_script]
        elif sys_name == "darwin" or sys_name == "linux":
            # CUPS `lp` — same command on Mac (built in) and Linux (installed
            # by default on most distros). `-d <name>` picks a specific
            # printer; without `-d`, system default is used.
            cmd = ["lp"]
            if printer_name:
                cmd += ["-d", printer_name]
            cmd.append(tmp_path)
        else:
            return False, f"unsupported_platform: {sys_name}"

        result = subprocess.run(cmd, capture_output=True, timeout=15)
        if result.returncode != 0:
            err = (result.stderr or b"").decode(errors="replace")[:200].strip()
            return False, f"print_failed (rc={result.returncode}): {err}"
        return True, ""
    except FileNotFoundError as e:
        return False, f"print_cmd_not_found: {e}"
    except subprocess.TimeoutExpired:
        return False, "print_cmd_timeout"
    except Exception as e:
        return False, f"system_print_failed: {e}"
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def submit_order(cart: list[dict], customer: dict, totals: dict,
                  profile: dict) -> dict:
    """Print the order ticket. Three printing modes, picked by what's
    configured in the customer's `order_submission` block:

      A. System printer (USB or network printer installed on the OS):
            order_submission:
                method: "print"
                printer_name: "Kitchen Printer"    # optional — omit for system default

         Uses Windows Out-Printer or CUPS `lp`. **Works for any printer
         installed on the customer's computer**, including USB-attached
         thermal printers, regular ink/laser printers, even PDF-to-file.
         No IP address required — the customer just plugs in their
         printer and configures it in Windows/Mac like any other
         printer. This is the recommended default.

      B. Network printer (TCP/IP on port 9100):
            order_submission:
                method: "print"
                printer_ip: "192.168.1.50"
                printer_port: 9100   # optional

         Uses python-escpos if installed, falls back to raw TCP. Best
         formatting (font size, cut command). For shops where the
         thermal printer sits on the LAN with its own static IP.

      C. (Either) — if BOTH printer_name and printer_ip are set, tries
         system printer first, falls back to network.

    Returns the same shape as the other dispatchers so dispatch_order
    can chain them."""
    cfg = (profile or {}).get("order_submission") or {}
    printer_name = (cfg.get("printer_name") or "").strip() or None
    printer_ip   = (cfg.get("printer_ip") or "").strip()
    printer_port = int(cfg.get("printer_port") or 9100)
    # `use_system_default = True` means use whatever printer the OS has
    # marked as default (zero config — works as long as the customer
    # has the right default set).
    use_system_default = bool(cfg.get("use_system_default", False))

    have_system_path = bool(printer_name or use_system_default)
    have_network_path = bool(printer_ip)
    if not (have_system_path or have_network_path):
        return {"ok": False, "error": "no_printer_configured",
                "method": "print",
                "hint": "Set order_submission.printer_name (the printer's "
                        "name as it shows up in Windows/Mac printer "
                        "settings) OR order_submission.printer_ip (the "
                        "printer's network IP) in the customer profile. "
                        "OR set order_submission.use_system_default = true "
                        "to use whatever printer the OS treats as default."}

    order_id = "ord_" + uuid.uuid4().hex[:12]
    lines = _build_ticket_lines(order_id, cart, customer, totals, profile)
    errors = []

    # Try system printer first (covers USB-attached printers + any
    # printer the customer has set up at the OS level — most common).
    if have_system_path:
        ok, err = _send_via_system_printer(printer_name, lines)
        if ok:
            log.info(
                f"order printed via system printer "
                f"({'default' if not printer_name else printer_name}) "
                f"(order_id={order_id}, total=${totals.get('total', 0):.2f})"
            )
            return {"ok": True, "order_id": order_id, "method": "print",
                    "to": (printer_name or "system default"),
                    "total": float(totals.get("total", 0)),
                    "transport": "system"}
        errors.append(f"system: {err}")

    # Fall back to network ESC/POS if printer_ip is configured.
    if have_network_path:
        ok, err = _send_via_escpos_network(printer_ip, printer_port, lines)
        if not ok:
            ok2, err2 = _send_via_raw_tcp(printer_ip, printer_port, lines)
            if ok2:
                log.info(
                    f"order printed via raw TCP to {printer_ip}:{printer_port} "
                    f"(order_id={order_id}, total=${totals.get('total', 0):.2f})"
                )
                return {"ok": True, "order_id": order_id, "method": "print",
                        "to": f"{printer_ip}:{printer_port}",
                        "total": float(totals.get("total", 0)),
                        "transport": "raw_tcp"}
            errors.append(f"network_escpos: {err}; network_raw: {err2}")
        else:
            log.info(
                f"order printed via escpos to {printer_ip}:{printer_port} "
                f"(order_id={order_id}, total=${totals.get('total', 0):.2f})"
            )
            return {"ok": True, "order_id": order_id, "method": "print",
                    "to": f"{printer_ip}:{printer_port}",
                    "total": float(totals.get("total", 0)),
                    "transport": "escpos"}

    return {"ok": False, "method": "print",
            "error": "all_print_paths_failed — " + " | ".join(errors)}
