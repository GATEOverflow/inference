#!/bin/bash

# Function to restart mkdocs serve
restart_mkdocs() {
    echo "Restarting mkdocs serve..."
    pkill -f "mkdocs serve"
    mkdocs serve --dev-addr=0.0.0.0:8001 &
}

# Watch main.py for changes
echo "Watching for changes in main.py..."
while true; do
    inotifywait -e modify main.py
    restart_mkdocs
done
