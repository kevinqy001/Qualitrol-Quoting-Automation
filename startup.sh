#!/bin/bash
# Startup command for Azure App Service (Linux, Python).
#
# Azure's default launcher (plain gunicorn) cannot run a FastAPI app because
# FastAPI is ASGI, not WSGI. We start gunicorn with the uvicorn worker class so
# it can serve the ASGI app exposed as `app` in app.py.
#
# In the Azure portal set:
#   Settings -> Configuration -> General settings -> Startup Command:
#       startup.sh
#
# Azure provides the port to bind to via $PORT (defaults to 8000 locally).
gunicorn app:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers 2 \
    --timeout 600 \
    --bind 0.0.0.0:${PORT:-8000}
