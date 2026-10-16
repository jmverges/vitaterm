"""`dsterm wsl-setup`: let a handheld on the LAN reach a server inside WSL2.

WSL is the one host where "it is on the LAN" is not automatically true, and
there are two networking modes with two different answers:

  mirrored   WSL shares Windows' addresses, but the Hyper-V firewall blocks
             inbound by default. Two Hyper-V firewall rules (TCP 8347, UDP
             8346 for discovery) and the handheld can find and reach it.
  nat        (the default) WSL is behind a private 172.x address. Windows has
             to forward TCP 8347 to it with a port proxy, and allow it through
             the Windows firewall. Broadcast discovery cannot cross NAT; the
             console's "Scan this network" finds the proxy instead.

This writes one PowerShell script, shows it, and -- only after you say yes --
runs it elevated (Windows asks for the UAC confirmation itself). It never
changes networkingMode: that is a reboot-level change to the machine, so it
is printed as a recommendation instead. The script is idempotent: it removes
its own rules by name before adding them.

Not testable outside Windows; the script text is unit-tested
(tests/test_cli.py) and the rest is a few subprocess calls.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

TCP_PORT = 8347
UDP_PORT = 8346
# The VMCreatorId Microsoft documents for WSL, used if the lookup by name finds
# nothing (older builds name it differently).
WSL_CREATOR_ID = "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}"


def networking_mode() -> str | None:
    """'mirrored', 'nat', or None when it cannot be told."""
    if shutil.which("wslinfo"):
        r = subprocess.run(["wslinfo", "--networking-mode"], capture_output=True, text=True)
        mode = r.stdout.strip().lower()
        if mode in ("nat", "mirrored"):
            return mode
    cfg = windows_profile_file(".wslconfig")
    if cfg is not None and cfg.exists():
        for line in cfg.read_text(errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == "networkingmode":
                return value.strip().lower() or "nat"
        return "nat"
    return None


def windows_profile_file(name: str) -> Path | None:
    if not shutil.which("cmd.exe") or not shutil.which("wslpath"):
        return None
    r = subprocess.run(["cmd.exe", "/c", "echo %USERPROFILE%"], capture_output=True,
                       text=True, cwd="/mnt/c" if Path("/mnt/c").is_dir() else None)
    win = r.stdout.strip()
    if not win or "%" in win:
        return None
    unix = subprocess.run(["wslpath", "-u", win], capture_output=True, text=True).stdout.strip()
    return Path(unix) / name if unix else None


def wsl_address() -> str | None:
    r = subprocess.run(["hostname", "-I"], capture_output=True, text=True)
    for addr in r.stdout.split():
        if "." in addr:
            return addr
    return None


def script(mode: str, wsl_ip: str | None) -> str:
    lines = [
        "# Written by `dsterm wsl-setup`. Runs elevated; safe to re-run.",
        "$ErrorActionPreference = 'Stop'",
        f"$tcp = {TCP_PORT}",
        f"$udp = {UDP_PORT}",
        f"Write-Host 'DS Claude Terminal: WSL networking mode is {mode}'",
    ]
    if mode == "mirrored":
        lines += [
            "$creator = (Get-NetFirewallHyperVVMCreator | Where-Object { $_.FriendlyName -eq 'WSL' }).VMCreatorId",
            f"if (-not $creator) {{ $creator = '{WSL_CREATOR_ID}' }}",
            "foreach ($name in 'DSTerm-In', 'DSTerm-Discovery') {",
            "    Remove-NetFirewallHyperVRule -Name $name -ErrorAction SilentlyContinue",
            "}",
            "New-NetFirewallHyperVRule -Name 'DSTerm-In' -DisplayName 'DS Terminal (WSL2)' "
            "-Direction Inbound -VMCreatorId $creator -Protocol TCP -LocalPorts $tcp "
            "-Action Allow | Out-Null",
            "New-NetFirewallHyperVRule -Name 'DSTerm-Discovery' -DisplayName "
            "'DS Terminal discovery (WSL2)' -Direction Inbound -VMCreatorId $creator "
            "-Protocol UDP -LocalPorts $udp -Action Allow | Out-Null",
            "Write-Host 'Hyper-V firewall: TCP 8347 and UDP 8346 allowed inbound to WSL'",
        ]
    else:
        if not wsl_ip:
            raise ValueError("NAT mode needs WSL's address for the port proxy")
        lines += [
            "netsh interface portproxy delete v4tov4 listenport=$tcp listenaddress=0.0.0.0 2>$null | Out-Null",
            f"netsh interface portproxy add v4tov4 listenport=$tcp listenaddress=0.0.0.0 "
            f"connectport=$tcp connectaddress={wsl_ip}",
            "Remove-NetFirewallRule -Name 'DSTerm-PortProxy' -ErrorAction SilentlyContinue",
            "New-NetFirewallRule -Name 'DSTerm-PortProxy' -DisplayName "
            "'DS Terminal (WSL2 port proxy)' -Direction Inbound -Protocol TCP "
            "-LocalPort $tcp -Action Allow -Profile Private,Domain | Out-Null",
            f"Write-Host 'Port proxy: Windows :8347 -> WSL {wsl_ip}:8347'",
            "Write-Host 'WSL changes address when it restarts: run dsterm wsl-setup again after a reboot.'",
            "Write-Host 'Discovery broadcast cannot cross NAT; on the console use Scan this network.'",
            "Write-Host 'Recommended: networkingMode=mirrored in .wslconfig avoids all of this.'",
        ]
    lines += [
        "Get-NetConnectionProfile | ForEach-Object {",
        "    Write-Host \"network '$($_.Name)' is $($_.NetworkCategory)\"",
        "    if ($_.NetworkCategory -eq 'Public') { Write-Host '  (set it to Private, or the rules above will not apply)' }",
        "}",
        "Get-NetIPAddress -AddressFamily IPv4 | Where-Object {",
        "    $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.InterfaceAlias -notlike 'vEthernet*'",
        "} | ForEach-Object { Write-Host \"  this PC: $($_.IPAddress)  ($($_.InterfaceAlias))\" }",
        "Read-Host 'Done. Press Enter to close'",
    ]
    return "\r\n".join(lines) + "\r\n"


def main(argv: list[str]) -> int:
    from .cli import die, is_wsl

    if not is_wsl():
        die("this is not WSL; nothing to set up")
    mode = networking_mode()
    if mode is None:
        die("cannot tell WSL's networking mode (no wslinfo, no readable .wslconfig)")
    ip = wsl_address()
    text = script(mode, ip)
    print(f"WSL networking mode: {mode}" + (f", WSL address {ip}" if ip else ""))
    print("\nThe script that will run elevated on Windows:\n")
    print(text.replace("\r\n", "\n"))
    if "--print" in argv:
        return 0
    if "--yes" not in argv:
        answer = input("Run it now? Windows will ask for permission. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("not run")
            return 1
    temp = windows_profile_file(r"AppData\Local\Temp")
    if temp is None:
        die("cannot find the Windows temp directory from WSL")
    target = Path(str(temp).replace("\\", "/")) / "dsterm-wsl-setup.ps1"
    target.write_text(text)
    win_path = subprocess.run(["wslpath", "-w", str(target)], capture_output=True,
                              text=True).stdout.strip()
    command = ("Start-Process powershell -Verb RunAs -Wait -ArgumentList "
               f"'-NoProfile','-ExecutionPolicy','Bypass','-File','{win_path}'")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command])
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
