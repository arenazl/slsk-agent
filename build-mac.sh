#!/bin/bash
echo "=== Build GrooveSync Agent (macOS) ==="
cd "$(dirname "$0")"
git pull
pkill -f GrooveSyncAgent 2>/dev/null
/Library/Frameworks/Python.framework/Versions/3.11/bin/pyinstaller -y GrooveSyncAgent.spec 2>&1 | tail -5
echo ""
echo "Build listo: dist/GrooveSyncAgent.app"
echo "Para lanzar: open dist/GrooveSyncAgent.app"
