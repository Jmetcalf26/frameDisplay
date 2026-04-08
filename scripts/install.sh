#!/bin/bash
set -e

echo "Installing system dependencies..."
sudo apt update
sudo apt install -y python3-pip python3-venv libportaudio2 libsndfile1 chromium-browser

echo "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "Installing Python dependencies..."
pip install -r requirements.txt

echo "Installing systemd service..."
sudo cp scripts/framedisplay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable framedisplay

echo ""
echo "Done! Next steps:"
echo "  1. Edit config.yaml with your Discogs API credentials"
echo "  2. Plug in your USB microphone"
echo "  3. Start with: sudo systemctl start framedisplay"
echo "  4. Open http://localhost:8080 in Chromium kiosk mode"
