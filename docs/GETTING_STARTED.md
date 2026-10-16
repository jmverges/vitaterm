# After setup

The first setup, step by step, and pairing are in the
[README](../README.md#setup). This page covers what comes after: updating,
starting WSL by itself on Windows, installing from source, and uninstalling.

## Updating

1. **The computer:** install the new `.whl` over the old one, then restart
   the server. Your Claude Code session keeps running.

   ```sh
   pipx install --force ./ds_claude_terminal-*.whl
   dsterm restart
   ```

2. **The DS:** copy the new `dsterm.nds` over the old one on the SD card, or
   use `dsterm card PATH --rom dsterm.nds`, which keeps a backup of the old
   one.

Update both. Pairings and settings are kept on both sides, so there is
nothing to redo. If only one side is updated, the DS says which side still
needs it ("the computer runs an older dsterm: update it there", or that the
card's `dsterm.nds` is older than the computer's) and connects by itself once
both are done.

## Start WSL when you sign in (Windows)

The README's Windows steps keep WSL running once it has started. To have it
start by itself when you sign in to Windows, run this once in **PowerShell**.
It runs as you, only while you are signed in, and needs no administrator:

```powershell
Register-ScheduledTask -TaskName "Start WSL for dsterm" -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Action (New-ScheduledTaskAction -Execute "wsl.exe" -Argument "--exec /bin/true") -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries)
```

It starts your default WSL distribution; if the server is in another one,
add `-d NAME` before `--exec`. To remove it:
`Unregister-ScheduledTask -TaskName "Start WSL for dsterm"`.

Why it's needed: Windows stops WSL when its last terminal window closes, and
a service running inside it does not count
([Microsoft's note](https://learn.microsoft.com/windows/wsl/systemd)); the
`.wslconfig` idle settings in the README turn that off
([Microsoft's settings](https://learn.microsoft.com/windows/wsl/wsl-config)).

## From source instead of the release

Clone this repository and run `pipx install .` in it, then `dsterm setup` as
in the README. To work on the code, run `./bin/dsterm setup` in the clone
instead: it makes its own virtual environment (it needs a `python3` 3.11 or
newer on your PATH) and runs the service from the checkout.
[CONTRIBUTING.md](../CONTRIBUTING.md) has the rest.

## Uninstalling

```sh
dsterm stop
tmux -L dsterm kill-server              # ends the Claude Code session it hosted
pipx uninstall ds-claude-terminal
rm -r ~/.config/ds-claude-terminal      # the keys and paired devices
```

Then remove the service file and its log:

- **Mac:** `rm ~/Library/LaunchAgents/dsterm.server.plist ~/Library/Logs/dsterm.log`
- **Linux and WSL:** `systemctl --user disable dsterm.service && rm ~/.config/systemd/user/dsterm.service`.
  Setup also turned on *lingering*, so user services run with no one logged
  in; if nothing else of yours needs it, `loginctl disable-linger`.

If you set it up from a clone, also `rm ~/.local/bin/dsterm`. On the card,
delete `dsterm.nds` and the `dsterm/` folder.

## Next

- [USING.md](USING.md): every screen, button and feature.
- [CUSTOMIZING.md](CUSTOMIZING.md): your own sounds, buttons and snippets.
- [HARDWARE.md](HARDWARE.md#the-network): if the network gives you trouble.
