
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
import ipaddress
import subprocess
import os

app = FastAPI()

def get_allowed_network():
    env_cidr = os.getenv("ALLOWED_NETWORK", "0.0.0.0/1")
    try:
        return ipaddress.ip_network(env_cidr)
    except ValueError:
        return None

ALLOWED_NETWORK = get_allowed_network()

def run_pull(image: str):
    if os.path.exists('/host/nix'):
        subprocess.run(['chroot', '/host', '/nix/var/nix/profiles/system/sw/bin/ctr', 'image', 'pull', image])
    else:
        subprocess.run(['chroot', '/host', '/usr/bin/ctr', 'image', 'pull', image])

def is_allowed_ip(remote_ip: str) -> bool:
    try:
        ip = ipaddress.ip_address(remote_ip)
        return ALLOWED_NETWORK is not None and ip in ALLOWED_NETWORK
    except ValueError:
        return False

@app.post("/pull-image")
async def pull_image(request: Request, background_tasks: BackgroundTasks):
    remote_ip = request.client.host
    if not is_allowed_ip(remote_ip):
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    image = data.get("image")
    if not image:
        raise HTTPException(status_code=400, detail="No image provided")

    background_tasks.add_task(run_pull, image)
    return {"status": "ok"}
