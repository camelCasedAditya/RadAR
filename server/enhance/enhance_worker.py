"""Job loop on the EC2 enhancer. The relay queues s3://<bucket>/jobs/<session name> and starts this
instance; this loop runs enhance.py for each job, removes the job, and when nothing has been queued
for IDLE seconds powers the machine off (the instance's shutdown behaviour is "stop", so the disk,
the venv, the model weights and the compiled gsplat kernels all survive to the next job).

    RADAR_S3_BUCKET=... python enhance_worker.py            # systemd runs this at boot
ponytail: a poll loop over an S3 prefix, not SQS; one instance, one job at a time.
"""
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "relay"))
from store import S3Store                                     # noqa: E402

IDLE = int(os.environ.get("RADAR_ENHANCE_IDLE", "180"))
JOBS = "jobs/"


def main():
    store = S3Store(os.environ["RADAR_S3_BUCKET"])
    out = pathlib.Path(os.environ.get("RADAR_MAPS_DIR", "/opt/radar/maps"))
    out.mkdir(parents=True, exist_ok=True)
    last = time.time()
    print(f"enhancer: watching s3://{store.bucket}/{JOBS} idle {IDLE}s", flush=True)
    while True:
        jobs = sorted(store.keys(JOBS))
        for key in jobs:
            name = key[len(JOBS):]
            if not name:
                continue
            print(f"enhancer: job {name}", flush=True)
            cmd = [sys.executable, str(HERE / "enhance.py"), name, "--out", str(out)]
            r = subprocess.run(cmd, env=os.environ)             # enhance.py writes the sidecar status itself
            print(f"enhancer: {name} exit {r.returncode}", flush=True)
            store.s3.delete_object(Bucket=store.bucket, Key=key)
            for p in out.glob(name + "*"):                      # keep the disk small
                p.unlink(missing_ok=True)
            last = time.time()
        if time.time() - last > IDLE and os.environ.get("RADAR_ENHANCE_NO_SHUTDOWN") != "1":
            print("enhancer: idle, powering off", flush=True)
            subprocess.run(["sudo", "shutdown", "-h", "now"])
            return
        time.sleep(10)


if __name__ == "__main__":
    main()
