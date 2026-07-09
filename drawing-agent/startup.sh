#!/bin/bash
# Startup command for Azure App Service (Linux) / container.
# FastAPI is ASGI, so run gunicorn with the uvicorn worker class.
# Azure provides the bind port via $PORT.
exec gunicorn app:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "${WEB_CONCURRENCY:-2}" \
    --timeout 600 \
    --bind "0.0.0.0:${PORT:-8080}"
