#!/bin/bash
# flash.sh — Copy dakbot project files to the ESP32-S3-ETH, then reboot it

PORT=${1:-/dev/ttyACM0}
FILES="config.py settings.py daktronics.py colorado.py webserver.py mqtt_publisher.py vmix.py updater.py version.py main.py daksports.json"

echo "Copying files to $PORT..."

# One mpremote session for every file: a separate connection per file means
# interrupting the running firmware and soft-resetting it once per file,
# which can leave the board's USB REPL unresponsive partway through (and a
# mix of old and new files on flash). 'reset' reboots into the new code.
# shellcheck disable=SC2086
if ! mpremote connect "$PORT" cp $FILES : + reset; then
    echo "ERROR: copy failed — press RESET on the board (or unplug/replug it) and run this again"
    exit 1
fi

echo "Done. The board is rebooting into the new code."
