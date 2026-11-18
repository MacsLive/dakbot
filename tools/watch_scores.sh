#!/bin/bash
# watch_scores.sh — Print a timestamped line each time the board's home/away
# score changes, polling its JSON endpoint 5x a second. The firmware keeps
# running, so you can watch vMix at the same time.
#
#   ./tools/watch_scores.sh 192.168.50.X        (Ctrl+C to stop)

BOARD=${1:?usage: $0 <board-ip>}
echo "Watching http://$BOARD/data — add points on the console. Ctrl+C to stop."

prev=""
fails=0
while true; do
    s=$(curl -s --max-time 1 "http://$BOARD/data" | python3 -c 'import sys,json; d=json.load(sys.stdin)[0]; print("home", d.get("HomeTeamScore"), " away", d.get("AwayTeamScore"))' 2>/dev/null)
    if [ -z "$s" ]; then
        fails=$((fails + 1))
        [ "$fails" -eq 5 ] && echo "No response from $BOARD — is this computer on the same network as the board?"
    else
        fails=0
        if [ "$s" != "$prev" ]; then
            echo "$(date +%T.%N | cut -c1-12)  $s"
            prev="$s"
        fi
    fi
    sleep 0.2
done
