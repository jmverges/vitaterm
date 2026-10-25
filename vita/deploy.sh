#!/bin/bash
# Quit vitaterm on the Vita, upload the new eboot and launch it (needs vitacompanion).
set -e
VITA=${VITA:-192.168.1.55}
cd "$(dirname "$0")"
echo "quit VTRM00001" | nc -q1 "$VITA" 1338 || true
sleep 2
curl -sS -T build/eboot.bin "ftp://$VITA:1337/ux0:/app/VTRM00001/eboot.bin"
echo "launch VTRM00001" | nc -q1 "$VITA" 1338
