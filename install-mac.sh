#!/bin/bash
echo "=== GrooveSync Agent - Instalador Mac ==="
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "Python3 no encontrado. Instalando..."
    xcode-select --install 2>/dev/null
    echo "Instala Python desde https://python.org/downloads y vuelve a correr este script."
    exit 1
fi

echo "Python3 encontrado: $(python3 --version)"

# Install dependencies
echo "Instalando dependencias..."
pip3 install --user pystray pillow aiohttp cloudinary 2>/dev/null || pip3 install pystray pillow aiohttp cloudinary

# Download agent
echo "Descargando agente..."
mkdir -p ~/.groovesync
curl -sL https://raw.githubusercontent.com/arenazl/slsk-agent/master/agent.py -o ~/.groovesync/agent.py

# Create launcher
cat > ~/.groovesync/start.command << 'LAUNCHER'
#!/bin/bash
cd ~/.groovesync
python3 agent.py
LAUNCHER
chmod +x ~/.groovesync/start.command

echo ""
echo "=== Instalacion completa ==="
echo "Para iniciar el agente, ejecuta:"
echo "  open ~/.groovesync/start.command"
echo ""
echo "O hace doble click en el archivo start.command en Finder:"
echo "  $(echo ~/.groovesync/start.command)"
echo ""

# Start agent
read -p "Iniciar el agente ahora? (s/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Ss]$ ]]; then
    python3 ~/.groovesync/agent.py
fi
