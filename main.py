
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
import ipaddress
import subprocess
import os
import threading
import shutil
import json
from datetime import datetime, timezone

app = FastAPI()
pull_lock = threading.Lock()
prune_lock = threading.Lock()

def get_allowed_network():
    env_cidr = os.getenv("ALLOWED_NETWORK", "0.0.0.0/1")
    try:
        return ipaddress.ip_network(env_cidr)
    except ValueError:
        return None

ALLOWED_NETWORK = get_allowed_network()

def run_pull(image: str):
    with pull_lock:
        if shutil.disk_usage('/host').free / (1024 ** 3) > 50:
            if os.path.exists('/host/nix'):
                subprocess.run(['chroot', '/host', '/nix/var/nix/profiles/system/sw/bin/crictl', 'pull', image])
            else:
                subprocess.run(['chroot', '/host', '/usr/bin/crictr', 'pull', image])
        else:
            print("Insufficient storage available")

def is_allowed_ip(remote_ip: str) -> bool:
    try:
        ip = ipaddress.ip_address(remote_ip)
        return ALLOWED_NETWORK is not None and ip in ALLOWED_NETWORK
    except ValueError:
        return False

def parse_rfc3339(ts: str) -> datetime:
    # Trim trailing Z and normalize fractional seconds to microseconds
    ts = ts.rstrip("Z")
    if "." in ts:
        base, frac = ts.split(".", 1)
        frac = (frac + "000000")[:6]
        ts = f"{base}.{frac}"
    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)

def get_used_images() -> set[str]:
    """
    Return a set of image references used by any container.
    """
    # List containers
    ps = run_in_host(['ps', '-a', '-q'])
    if ps.returncode != 0:
        print("crictl ps failed:", ps.stderr)
        return set()

    ids = ps.stdout.strip().splitlines()
    if not ids:
        return set()

    # Inspect each container individually for robustness
    used = set()
    for cid in ids:
        insp = run_in_host(['inspect', cid])
        if insp.returncode != 0:
            continue
        try:
            obj = json.loads(insp.stdout)
        except json.JSONDecodeError:
            continue
        ref = obj.get("status", {}).get("imageRef")
        if ref:
            used.add(ref)
    return used

def get_all_images() -> list[str]:
    img = run_in_host(['images', '-q'])
    if img.returncode != 0:
        print("crictl images failed:", img.stderr)
        return []
    return img.stdout.strip().splitlines()

def get_image_created(img_id: str) -> datetime | None:
    """
    Return creation time from .info.imageSpec.created for an image, or None.
    """
    insp = run_in_host(['inspecti', img_id])
    if insp.returncode != 0:
        print(f"inspecti failed for {img_id}:", insp.stderr)
        return None
    try:
        data = json.loads(insp.stdout)
    except json.JSONDecodeError:
        return None

    created = (
        data.get("info", {})
            .get("imageSpec", {})
            .get("created")
    )
    if not created:
        return None
    try:
        return parse_rfc3339(created)
    except Exception:
        return None

def run_in_host(cmd):
    """
    Helper to run commands in /host chroot, picking the right crictl binary.
    cmd is a list starting with the tool name, e.g. ['crictl', 'pull', image]
    """
    if os.path.exists('/host/nix'):
        full_cmd = ['chroot', '/host', '/nix/var/nix/profiles/system/sw/bin/crictl'] + cmd
    else:
        full_cmd = ['chroot', '/host', '/usr/bin/crictl'] + cmd
    return subprocess.run(full_cmd, check=False, capture_output=True, text=True)

def run_prune(days: int = 14):
    """
    Prune images that are:
    - older than `days`
    - not used by any container
    """
    with prune_lock:
        now = datetime.now(timezone.utc)
        cutoff_seconds = days * 24 * 60 * 60

        used_images = get_used_images()
        all_images = get_all_images()

        for img in all_images:
            created_dt = get_image_created(img)
            if not created_dt:
                continue

            age_seconds = (now - created_dt).total_seconds()
            if age_seconds < cutoff_seconds:
                continue

            # If the image id/digest string appears in used images, skip
            if img in used_images:
                continue

            print(f"Pruning image {img}, age={age_seconds/86400:.1f} days")
            r = run_in_host(['rmi', img])
            if r.returncode != 0:
                print(f"Failed to remove {img}: {r.stderr}")

@app.post("/pull-image")
async def pull_image(request: Request, background_tasks: BackgroundTasks):
    remote_ip = request.client.host
    if not is_allowed_ip(remote_ip):
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    image = data.get("image")
    if not image:
        raise HTTPException(status_code=400, detail="No image provided")
 
    if image.count('/') <= 1 and not image.startswith('docker.io/'):
        image = "docker.io/" + image

    background_tasks.add_task(run_pull, image)
    return {"status": "ok"}

@app.post("/prune-images")
async def prune_images(request: Request, background_tasks: BackgroundTasks):
    """
    Trigger pruning of unused images older than 14 days.
    Optional JSON body: {"days": 14}
    """
    remote_ip = request.client.host
    if not is_allowed_ip(remote_ip):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        data = await request.json()
    except Exception:
        data = {}

    days = data.get("days", 14)
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid days value")

    background_tasks.add_task(run_prune, days)
    return {"status": "ok", "days": days}
