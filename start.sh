#!/bin/bash

echo "Activate the eye controller..."

# Check virtual environment
if [ -d "$HOME/mediapipe_env" ]; then
    echo "Activate virtual environment..."
    source "$HOME/mediapipe_env/bin/activate"
else
    echo "Virtual environment not found, using system Python environment"
fi

echo "Starting application..."
python3 ./src/main.py

echo "Application exited"