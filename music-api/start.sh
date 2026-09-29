#!/bin/sh
# Run the key bot in the background (only if BOT_TOKEN is set), then the API in the foreground.
if [ -n "$BOT_TOKEN" ]; then
  python bot.py &
fi
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
