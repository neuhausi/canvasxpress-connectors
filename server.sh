#!/usr/bin/env bash
# Manage the connectors BYO-database demo (uvicorn on 127.0.0.1:8300).
# Usage: ./server.sh start|stop|restart|status
DIR="$(cd "$(dirname "$0")/examples/byo_database" && pwd)"
UVICORN="$(cd "$(dirname "$0")" && pwd)/.venv/bin/uvicorn"
PIDFILE="$DIR/server.pid"
case "$1" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then echo "already running"; exit 0; fi
    cd "$DIR" && nohup "$UVICORN" app:app --host 127.0.0.1 --port 8300 > server.log 2>&1 &
    echo $! > "$PIDFILE"; echo "started (pid $(cat "$PIDFILE"))";;
  stop)
    [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null; rm -f "$PIDFILE"; echo "stopped";;
  restart) "$0" stop; sleep 1; "$0" start;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then echo "running (pid $(cat "$PIDFILE"))"; else echo "not running"; fi;;
  *) echo "usage: $0 start|stop|restart|status"; exit 2;;
esac
