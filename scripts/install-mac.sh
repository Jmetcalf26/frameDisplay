#!/bin/bash
set -e

if ! command -v brew &> /dev/null; then
    echo "Error: Homebrew is required. Install it from https://brew.sh"
    exit 1
fi

echo "Installing system dependencies via Homebrew..."
brew install portaudio libsndfile

echo "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "Installing Python dependencies..."
pip install -r requirements.txt

echo ""
echo "Done! Next steps:"
echo "  1. cp config.example.yaml config.yaml"
echo "  2. Edit config.yaml with your Discogs API credentials"
echo "  3. source venv/bin/activate && python run.py"
echo "  4. Open http://localhost:8080 in your browser"
echo ""
echo "Note: macOS will prompt for microphone permission on first run."
