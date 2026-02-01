
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
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Configure debug mode via environment variable
DEBUG_MODE = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

# Configure pruning schedule via environment variable
PRUNE_SCHEDULE = os.getenv("PRUNE_SCHEDULE", "")
PRUNE_DAYS = int(os.getenv("PRUNE_DAYS", "14"))

# Configure console logging
console_handler = logging.StreamHandler()
if DEBUG_MODE:
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    console_handler.setLevel(logging.DEBUG)
else:
    console_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
    console_handler.setLevel(logging.INFO)

# Configure logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
logger.addHandler(console_handler)

# Define image lock
image_lock = threading.Lock()

def is_container() -> bool:
    """
    Detect if running inside a container using multiple methods.
    Returns True if inside a container, False otherwise.
    """
    # Method 1: Check /proc/1/cgroup for container markers
    try:
        with open('/proc/1/cgroup', 'r') as f:
            cgroup_content = f.read()
            # Check for common container markers
            if 'docker' in cgroup_content or 'kubepods' in cgroup_content or 'kublet' in cgroup_content:
                return True
    except (FileNotFoundError, IOError):
        pass
    
    # Method 2: Check for .dockerenv file
    if os.path.exists('/.dockerenv'):
        return True
    
    # Method 3: Check Kubernetes environment variables
    if os.environ.get('KUBERNETES_SERVICE_HOST'):
        return True
    
    # Method 4: Check Docker environment variables
    for env_var in os.environ.keys():
        if env_var.startswith('DOCKER_') or env_var.startswith('containerd_'):
            return True
    
    return False

# Cache container detection result
IN_CONTAINER = is_container()
logger.info(f"Running in container: {IN_CONTAINER}")

def get_allowed_network():
    env_cidr = os.getenv("ALLOWED_NETWORK", "0.0.0.0/1")
    try:
        return ipaddress.ip_network(env_cidr)
    except ValueError:
        return None

ALLOWED_NETWORK = get_allowed_network()

# Initialize scheduler
scheduler = BackgroundScheduler(timezone=timezone.utc)

def run_prune_job():
    """
    Wrapper function for scheduled prune operations.
    Uses the configured PRUNE_DAYS threshold.
    """
    logger.info(f"Scheduled prune operation started (days threshold: {PRUNE_DAYS})")
    run_prune(days=PRUNE_DAYS)

async def configure_scheduler():
    """
    Configure the APIScheduler with cron job if PRUNE_SCHEDULE is set.
    """
    if PRUNE_SCHEDULE:
        try:
            trigger = CronTrigger.from_crontab(PRUNE_SCHEDULE)
            scheduler.add_job(
                run_prune_job,
                trigger=trigger,
                id="image_prune_job",
                replace_existing=True
            )
            logger.info(f"Cron scheduler configured successfully with expression: {PRUNE_SCHEDULE}")
            scheduler.start()
            logger.info("Image prune scheduler started")
        except Exception as e:
            logger.error(f"Failed to configure scheduler with cron expression '{PRUNE_SCHEDULE}': {str(e)}")
            logger.warning("Prune scheduler will not run; manual operation only")

def cleanup_scheduler():
    """
    Cleanup scheduler resources on shutdown.
    """
    if scheduler.running:
        scheduler.remove_all_jobs()
        scheduler.shutdown(wait=True)
        logger.info("Image prune scheduler stopped")

# Log startup
logger.info("Image puller service started")
logger.info(f"Allowed network configured: {ALLOWED_NETWORK}")
logger.info(f"Container detection: {IN_CONTAINER}")
logger.info(f"Prune schedule configured: {PRUNE_SCHEDULE or 'Disabled'}")

def run_pull(image: str):
    logger.debug(f"Lock acquired for pull operation on image: {image}")
    with image_lock:
        logger.debug(f"Lock acquired for pull operation on image: {image}")
        try:
            disk_path = '/host' if IN_CONTAINER else '/'
            if shutil.disk_usage(disk_path).free / (1024 ** 3) > 50:
                result = run_in_host(['pull', image])
                
                if result.returncode == 0:
                    logger.info(f"Successfully pulled image: {image}")
                    # Try to get image creation time for logging
                    created_time = get_image_created(image)
                    if created_time:
                        logger.debug(f"  Image creation time: {created_time.isoformat()}")
                    else:
                        logger.warning("Image creation time missing")
                else:
                    logger.error(f"Failed to pull image {image}: Command returned code {result.returncode}, stderr: {result.stderr}")
            else:
                logger.warning(f"Insufficient storage available for pulling image: {image}")
        except subprocess.TimeoutExpired:
            logger.error(f"Pull operation timed out for image: {image}, command was blocked after 30 minutes")
        except FileNotFoundError as e:
            logger.error(f"Required binary not found during pull for image {image}: {str(e)}")
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
    ps = run_in_host(['ps', '-a', '-q'])
    if ps.returncode != 0:
        logger.error(f"crictl ps failed: {ps.stderr}")
        return set()

    ids = ps.stdout.strip().splitlines()
    if not ids:
        logger.debug("No running containers found")
        return set()

    used = set()
    for cid in ids:
        logger.debug(f"Checking container ID: {cid}")
        insp = run_in_host(['inspect', cid])
        if insp.returncode != 0:
            logger.debug(f"Failed to inspect container {cid}")
            continue
        obj = json.loads(insp.stdout)
        ref = obj.get("status", {}).get("imageRef")
        if ref:
            logger.debug(f"Container {cid} uses image: {ref}")
            used.add(ref)
    logger.info(f"Identified {len(used)} used images")
    return used

def get_all_images() -> list[str]:
    logger.debug("Fetching list of all images")
    img = run_in_host(['images', '-q'])
    if img.returncode != 0:
        logger.error(f"crictl images failed: {img.stderr}")
        return []
    image_list = img.stdout.strip().splitlines()
    logger.debug(f"Found {len(image_list)} images to check: {', '.join(image_list[:10])}{'...' if len(image_list) > 10 else ''}")
    return image_list

def get_image_created(img_id: str) -> datetime | None:
    """
    Return creation time from .info.imageSpec.created for an image, or None.
    """
    insp = run_in_host(['inspecti', img_id])
    if insp.returncode != 0:
        logger.error(f"Inspect failed for image {img_id}: {insp.stderr}")
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
    if IN_CONTAINER:
        if os.path.exists('/host/nix'):
            full_cmd = ['chroot', '/host', '/nix/var/nix/profiles/system/sw/bin/crictl'] + cmd
        else:
            full_cmd = ['chroot', '/host', '/usr/bin/crictl'] + cmd
    else:
        if os.path.exists('/nix/var/nix/profiles/system/sw/bin/crictl'):
            full_cmd = ['/nix/var/nix/profiles/system/sw/bin/crictl'] + cmd
        else:
            full_cmd = ['/usr/bin/crictl'] + cmd
    try:
        result = subprocess.run(full_cmd, check=False, capture_output=True, text=True)
        logger.debug(f"Command completed with return code {result.returncode}")
        if result.stderr:
            logger.debug(f"Command stderr: {result.stderr}")
        if result.stdout:
            logger.debug(f"Command stdout: {result.stdout}")
        return result
    except Exception as e:
        logger.debug(f"Error executing command {' '.join(cmd)}: {str(e)}")
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
    
    with image_lock:
        logger.debug(f"Lock acquired for pruning operation with {days} days threshold")
        now = datetime.now(timezone.utc)
        cutoff_seconds = days * 24 * 60 * 60

        used_images = get_used_images()
        all_images = get_all_images()
        logger.info(f"Found {len(all_images)} total images, {len(used_images)} in use")

        for img in all_images:
            logger.debug(f"Processing image: {img}")
            created_dt = get_image_created(img)
            if not created_dt:
                logger.warning(f"Could not determine creation time for image: {img}")
                continue

            age_seconds = (now - created_dt).total_seconds()
            if age_seconds < cutoff_seconds:
                logger.debug(f"Skipping image: {img}, reason: too new ({age_seconds/86400:.1f} days < {days} days)")
                continue

            if img in used_images:
                logger.debug(f"Skipping image: {img}, reason: in use by running container")
                continue

            logger.debug(f"Attempting to prune image: {img}, age={age_seconds/86400:.1f} days, created={created_dt.isoformat()}")
            r = run_in_host(['rmi', img])
            if r.returncode != 0:
                logger.error(f"Failed to remove image {img}: Command returned code {r.returncode}, stderr: {r.stderr}")
                errors += 1
            else:
                logger.info(f"Successfully pruned image: {img}, created: {created_dt.isoformat()}")
                images_pruned += 1

    logger.info(f"Pruning operation completed. Removed {images_pruned} images, {errors} errors")

# Configure FastAPI app
app = FastAPI(
    title="Container Image Puller Service",
    description="Service for pulling and pruning container images",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

@app.on_event("startup")
async def startup_event():
    """
    Handle application startup events.
    """
    await configure_scheduler()
    logger.info("Application startup complete")

@app.on_event("shutdown")
async def shutdown_event():
    """
    Handle application shutdown events.
    """
    cleanup_scheduler()
    logger.info("Application shutdown complete")

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

    days = 14
    try:
        data = await request.json()
        if data:
            days = data.get("days", 14)
            try:
                days = int(days)
            except (TypeError, ValueError) as e:
                logger.warning(f"Prune request failed - Invalid days value: {days} from IP: {remote_ip}: {str(e)}")
                raise HTTPException(status_code=400, detail="Invalid days value")
    except Exception as e:
        logger.info(f"Prune request received without JSON body or with invalid JSON from IP: {remote_ip}")
        days = 14

    logger.info(f"Starting prune operation with {days} days threshold from IP: {remote_ip}")
    try:
        background_tasks.add_task(run_prune, days)
        return {"status": "ok", "days": days}
    except Exception as e:
        logger.error(f"Failed to start prune operation from IP {remote_ip}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to start prune operation: {str(e)}")
