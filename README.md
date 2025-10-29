# Container Image Puller

Simple API to pull container images.

## Setup

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: ${APP_NAME}
  namespace: ${APP_NAMESPACE}
spec:
  selector:
    matchLabels:
      app: ${APP_NAME}
  template:
    metadata:
      labels:
        app: ${APP_NAME}
    spec:
      hostNetwork: false
      hostPID: true
      hostIPC: true
      containers:
      - name: ${APP_NAME}
        image: ghcr.io/niki-on-github/container-image-puller:v0.0.3
        securityContext:
          privileged: true
        volumeMounts:
        - mountPath: /host
          name: host
        ports:
        - containerPort: 8080
      volumes:
      - name: host
        hostPath:
          path: /
---
apiVersion: v1
kind: Service
metadata:
  name: ${APP_NAME}
  namespace: ${APP_NAMESPACE}
spec:
  selector:
    app: ${APP_NAME}
  ports:
  - protocol: TCP
    port: 80
    targetPort: 8080
  type: ClusterIP
```

## Usage

```yaml
name: Pull Container Images

on:
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    container:
      image: "curlimages/curl:latest"
    steps:
      - name: Pull Image on Host
        run: |
          curl -s -X POST http://image-puller.system.svc.cluster.local/pull-image \
            -H "Content-Type: application/json" \
            -d '{"image": "docker.io/tailscale/tailscale:latest"}'
```
