#!/usr/bin/env bash

 curl -X POST http://127.0.0.1:8000/pull-image \
      -H "Content-Type: application/json" \
      -d '{"image": "docker.io/tailscale/tailscale:latest"}'
