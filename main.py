
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
import traceback
import ipaddress
import subprocess
import os
import threading
import shutil
import json
import logging
from datetime import datetime, timezone

# Configure console logging
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(
    '%(levelname)s - %(message)s'
))

# Configure logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(console_handler)

# Define image lock
image_lock = threading.Lock()

def get_allowed_network():
    env_cidr = os.getenv("ALLOWED_NETWORK", "0.0.0.0/1")
    try:
        return ipaddress.ip_network(env_cidr)
    except ValueError:
        return None

ALLOWED_NETWORK = get_allowed_network()

# Log startup
logger.info("Image puller service started")
logger.info(f"Allowed network configured: {ALLOWED_NETWORK}")

def run_pull(image: str):
    logger.debug(f"Lock acquired for pull operation on image: {image}")
    with image_lock:
        logger.debug(f"Pull operation started for image: {image}")
        try:
            if shutil.disk_usage('/host').free / (1024 ** 3) > 50:
                if os.path.exists('/host/nix'):
                    result = subprocess.run(['chroot', '/host', '/nix/var/nix/profiles/system/sw/bin/crictl', 'pull', image], timeout=30*60)
                else:
                    result = subprocess.run(['chroot', '/host', '/usr/bin/crictl', 'pull', image], timeout=30*60)
                
                if result.returncode == 0:
                    logger.info(f"Successfully pulled image: {image}")
                else:
                    logger.error(f"Failed to pull image {image}: Command returned code {result.returncode}, stderr: {result.stderr}")
                    print(f"Failed to pull image {image}: {result.stderr}")
            else:
                logger.warning(f"Insufficient storage available for pulling image: {image}")
                print("Insufficient storage available")
        except subprocess.TimeoutExpired:
            logger.error(f"Pull operation timed out for image: {image}, command was blocked after 30 minutes")
        except FileNotFoundError as e:
            logger.error(f"Required binary not found during pull for image {image}: {str(e)}")
            print(f"Required binary not found: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error during pull for {image}: {str(e)}\n{traceback.format_exc()}")
    logger.debug(f"Lock released after pull operation for image: {image}")

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
    try:
        ps = run_in_host(['ps', '-a', '-q'])
        if ps.returncode != 0:
            logger.error(f"crictl ps failed: {ps.stderr}")
            print("crictl ps failed:", ps.stderr)
            return set()
    except RuntimeError as e:
        logger.error(f"Failed to list containers: {str(e)}")
        print(f"Failed to list containers: {str(e)}")
        return set()

    ids = ps.stdout.strip().splitlines()
    if not ids:
        return set()

    # Inspect each container individually for robustness
    used = set()
    for cid in ids:
        try:
            insp = run_in_host(['inspect', cid])
            if insp.returncode != 0:
                continue
            try:
                obj = json.loads(insp.stdout)
            except json.JSONDecodeError as e:
                continue
            ref = obj.get("status", {}).get("imageRef")
            if ref:
                used.add(ref)
        except RuntimeError as e:
            logger.warning(f"Failed to inspect container {cid}: {str(e)}")
            continue
    logger.info(f"Identified {len(used)} used images")
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
    insp = run_in_host(['inspect', img_id])
    if insp.returncode != 0:
        print(f"inspect failed for {img_id}:", insp.stderr)
        return None
    try:
        data = json.loads(insp.stdout)
    except json.JSONDecodeError as e:
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
    try:
        result = subprocess.run(full_cmd, check=False, capture_output=True, text=True)
        logger.debug(f"Command completed with return code {result.returncode}")
        if result.stderr:
            logger.debug(f"Command stderr: {result.stderr}")
        if result.stdout:
            logger.debug(f"Command stdout: {result.stdout}")
        return result
    except Exception as e:
        logger.error(f"Error executing command {' '.join(cmd)}: {str(e)}")
        raise RuntimeError(f"Command execution failed: {str(e)}")

def run_prune(days: int = 14):
    """
    Prune images that are:
    - older than `days`
    - not used by any container
    """
    logger.info(f"Pruning operation started with {days} days threshold")
    images_pruned = 0
    errors = 0
    
    try:
        with image_lock:
            logger.debug(f"Lock acquired for pruning operation with {days} days threshold")
            now = datetime.now(timezone.utc)
            cutoff_seconds = days * 24 * 60 * 60

            used_images = get_used_images()
            all_images = get_all_images()
            logger.info(f"Found {len(all_images)} total images, {len(used_images)} in use")

            for img in all_images:
                try:
                    logger.debug(f"Processing image: {img}")
                    created_dt = get_image_created(img)
                    if not created_dt:
                        logger.warning(f"Could not determine creation time for image: {img}")
                        continue

                    age_seconds = (now - created_dt).total_seconds()
                    if age_seconds < cutoff_seconds:
                        logger.debug(f"Image {img} is too new ({age_seconds/86400:.1f} days < {days} days), skipping")
                        continue

                    # If the image id/digest string appears in used images, skip
                    if img in used_images:
                        logger.debug(f"Skipping used image: {img}")
                        continue

                    logger.debug(f"Attempting to prune image {img}, age={age_seconds/86400:.1f} days")
                    print(f"Pruning image {img}, age={age_seconds/86400:.1f} days")
                    r = run_in_host(['rmi', img])
                    if r.returncode != 0:
                        logger.error(f"Failed to remove image {img}: Command returned code {r.returncode}, stderr: {r.stderr}")
                        print(f"Failed to remove {img}: {r.stderr}")
                        errors += 1
                    else:
                        logger.info(f"Successfully pruned image: {img}")
                        images_pruned += 1
                except RuntimeError as e:
                    logger.error(f"Error processing image {img}: {str(e)}")
                    errors += 1

        logger.info(f"Pruning operation completed. Removed {images_pruned} images, {errors} errors")
    except Exception as e:
        logger.error(f"Fatal error during pruning: {str(e)}\n{traceback.format_exc()}")
        raise

# Configure FastAPI app
app = FastAPI(
    title="Container Image Puller Service",
    description="Service for pulling and pruning container images",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

@app.post("/pull-image")
async def pull_image(request: Request, background_tasks: BackgroundTasks):
    remote_ip = request.client.host
    if not is_allowed_ip(remote_ip):
        logger.warning(f"Blocked pull attempt from IP: {remote_ip}")
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    image = data.get("image")
    if not image:
        logger.warning(f"Pull request failed - No image provided from IP: {remote_ip}")
        raise HTTPException(status_code=400, detail="No image provided")

    original_image = image
    if image.count('/') <= 1 and not image.startswith('docker.io/'):
        image = "docker.io/" + image
        logger.info(f"Added docker.io prefix to image: {original_image}")

    if not image or not image.strip():
        logger.warning(f"Pull request failed - Invalid image name: {image}")
        raise HTTPException(status_code=400, detail="Invalid image name")

    background_tasks.add_task(run_pull, image)
    logger.info(f"Background pull task added for image: {image} from IP: {remote_ip}")
    return {"status": "ok", "message": "Pull request received and will be processed in background"}

@app.post("/prune-images")
async def prune_images(request: Request, background_tasks: BackgroundTasks):
    """
    Trigger pruning of unused images older than 14 days.
    Optional JSON body: {"days": 14}
    """
    remote_ip = request.client.host
    if not is_allowed_ip(remote_ip):
        logger.warning(f"Blocked prune attempt from IP: {remote_ip}")
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        data = await request.json()
    except Exception as e:
        logger.warning(f"Prune request failed - JSON parsing error from IP: {remote_ip}: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid JSON request")

    days = data.get("days", 14)
    try:
        days = int(days)
    except (TypeError, ValueError) as e:
        logger.warning(f"Prune request failed - Invalid days value: {days} from IP: {remote_ip}: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid days value")

    logger.info(f"Starting prune operation with {days} days threshold from IP: {remote_ip}")
    try:
        background_tasks.add_task(run_prune, days)
        return {"status": "ok", "days": days}
    except Exception as e:
        logger.error(f"Failed to start prune operation from IP {remote_ip}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to start prune operation: {str(e)}")
