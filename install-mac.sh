#!/bin/bash
echo "=== GrooveSync Agent - Instalador Mac ==="
echo ""

# Check Python version
if ! command -v python3 &> /dev/null; then
    echo "Python3 no encontrado."
    echo "Instalalo desde https://www.python.org/downloads/ y volve a correr este script."
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo $PY_VERSION | cut -d. -f1)
PY_MINOR=$(echo $PY_VERSION | cut -d. -f2)

echo "Python $PY_VERSION encontrado"

if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]); then
    echo "Se necesita Python 3.9 o superior. Tenes $PY_VERSION."
    echo "Instala una version mas nueva desde https://www.python.org/downloads/"
    exit 1
fi

# Create directory
mkdir -p ~/.groovesync

# Create venv
echo "Creando entorno virtual..."
python3 -m venv ~/.groovesync/venv
source ~/.groovesync/venv/bin/activate

# Install dependencies
echo "Instalando dependencias..."
pip install --upgrade pip 2>/dev/null
pip install pystray pillow aiohttp cloudinary aioslsk 2>/dev/null

# Install FFmpeg via Homebrew if available
if command -v brew &> /dev/null; then
    if ! command -v ffmpeg &> /dev/null; then
        echo "Instalando FFmpeg..."
        brew install ffmpeg 2>/dev/null
    else
        echo "FFmpeg ya instalado"
    fi
else
    if ! command -v ffmpeg &> /dev/null; then
        echo "FFmpeg no encontrado. Para el mix editor, instala Homebrew (https://brew.sh) y despues: brew install ffmpeg"
    fi
fi

# Download agent
echo "Descargando agente..."
curl -sL https://raw.githubusercontent.com/arenazl/slsk-agent/master/agent.py -o ~/.groovesync/agent.py

# Create launcher script
cat > ~/.groovesync/GrooveSyncAgent.command << 'LAUNCHER'
#!/bin/bash
source ~/.groovesync/venv/bin/activate
python ~/.groovesync/agent.py
LAUNCHER
chmod +x ~/.groovesync/GrooveSyncAgent.command

# Create updater
cat > ~/.groovesync/update.command << 'UPDATER'
#!/bin/bash
echo "Actualizando GrooveSync Agent..."
source ~/.groovesync/venv/bin/activate
pip install --upgrade pystray pillow aiohttp cloudinary aioslsk 2>/dev/null
curl -sL https://raw.githubusercontent.com/arenazl/slsk-agent/master/agent.py -o ~/.groovesync/agent.py
echo "Actualizado. Reinicia el agente."
UPDATER
chmod +x ~/.groovesync/update.command

echo ""
echo "=== Instalacion completa ==="
echo ""
echo "Para iniciar: doble click en ~/.groovesync/GrooveSyncAgent.command"
echo "Para actualizar: doble click en ~/.groovesync/update.command"
echo ""

# Start agent
read -p "Iniciar el agente ahora? (s/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Ss]$ ]]; then
    python ~/.groovesync/agent.py
fi
