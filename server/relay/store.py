"""S3 copy of the session files. Enabled when RADAR_S3_BUCKET is set (credentials from the usual
AWS env/config). Everything the relay appends to the current session file is also streamed to
s3://<bucket>/sessions/<name> as a multipart upload in 8 MB parts from one background thread, so
the event loop never waits on the network; the session ends with a small sidecar
sessions/<name>.json {patches, seconds, bytes}. Local files that never made it up (relay crashed
mid-session) are uploaded whole at the next start. ponytail: one thread and a queue; a part
smaller than 8 MB only goes up when the session ends, the local file is the safety net until then.
"""
import json
import os
import queue
import threading
import time

PART = 8 << 20
PREFIX = "sessions/"


class S3Store:
    def __init__(self, bucket):
        import boto3
        from botocore.config import Config
        region = os.environ.get("AWS_DEFAULT_REGION") or boto3.session.Session().region_name or "us-east-2"
        # presigned URLs only verify when signed for the bucket's regional endpoint
        self.bucket = bucket
        self.s3 = boto3.client("s3", region_name=region, endpoint_url=f"https://s3.{region}.amazonaws.com",
                               config=Config(signature_version="s3v4"))
        self.q = queue.Queue()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    # ---- called from the relay (event loop) ----
    def begin(self, name): self.q.put(("begin", name))
    def write(self, b): self.q.put(("data", b))
    def finish(self, meta): self.q.put(("finish", meta))
    def sync_file(self, path, meta): self.q.put(("file", (path, meta)))

    def close(self, timeout=60):
        self.q.put(("stop", None))
        self.t.join(timeout)

    def listing(self):
        """{name: meta} for finished sessions in the bucket."""
        out = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=PREFIX):
            for o in page.get("Contents", []):
                k = o["Key"][len(PREFIX):]
                if k.endswith(".json"):
                    out[k[:-5]] = json.loads(self.s3.get_object(Bucket=self.bucket, Key=o["Key"])["Body"].read())
        return out

    def url(self, name, seconds=3600):
        return self.s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": PREFIX + name}, ExpiresIn=seconds)

    def delete(self, name):
        """Remove a session and everything derived from it (sidecar, enhanced mesh/splat files)."""
        keys = [{"Key": k} for k in self.keys(PREFIX + name)]
        if keys:
            self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})

    # ---- small synchronous helpers (enhance job, relay enhance endpoint) ----
    def keys(self, prefix):
        return [o["Key"] for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix)
                for o in page.get("Contents", [])]

    def get_json(self, key):
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())
        except self.s3.exceptions.NoSuchKey:
            return None

    def put_json(self, key, obj):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(obj).encode(), ContentType="application/json")

    def put_file(self, path, key):
        self.s3.upload_file(str(path), self.bucket, key)

    def get_file(self, key, path):
        self.s3.download_file(self.bucket, key, str(path))

    # ---- background thread ----
    def _run(self):
        key = upload_id = None
        parts, buf = [], bytearray()

        def flush():
            nonlocal buf
            if not buf:
                return
            r = self.s3.upload_part(Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumber=len(parts) + 1, Body=bytes(buf))
            parts.append({"ETag": r["ETag"], "PartNumber": len(parts) + 1})
            buf = bytearray()

        def end(meta):
            nonlocal key, upload_id, parts
            if upload_id is None:
                return
            try:
                if (not parts and not buf) or not meta.get("mapped"):     # nothing mapped: not worth keeping
                    self.s3.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
                else:
                    flush()
                    self.s3.complete_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts})
                    self.s3.put_object(Bucket=self.bucket, Key=key + ".json", Body=json.dumps(meta).encode(), ContentType="application/json")
                    print(f"s3: finished {key} ({meta['bytes'] / 1e6:.1f} MB)", flush=True)
            except Exception as e:
                print(f"s3: finishing {key} failed: {e!r}", flush=True)
            key = upload_id = None
            parts = []

        while True:
            cmd, arg = self.q.get()
            try:
                if cmd == "begin":
                    key = PREFIX + arg
                    upload_id = self.s3.create_multipart_upload(Bucket=self.bucket, Key=key)["UploadId"]
                    parts, buf = [], bytearray()
                elif cmd == "data" and upload_id:
                    buf += arg
                    if len(buf) >= PART:
                        flush()
                elif cmd == "finish":
                    end(arg)
                elif cmd == "file":
                    path, meta = arg
                    k = PREFIX + os.path.basename(path)
                    self.s3.upload_file(str(path), self.bucket, k)
                    self.s3.put_object(Bucket=self.bucket, Key=k + ".json", Body=json.dumps(meta).encode(), ContentType="application/json")
                    print(f"s3: synced {k}", flush=True)
                elif cmd == "stop":
                    end({"patches": 0, "seconds": 0, "bytes": 0}) if upload_id else None
                    return
            except Exception as e:
                print(f"s3: {cmd} failed: {e!r}", flush=True)
                if cmd == "begin":
                    key = upload_id = None


if __name__ == "__main__":
    # smoke test against the real bucket: needs RADAR_S3_BUCKET + credentials
    b = os.environ["RADAR_S3_BUCKET"]
    st = S3Store(b)
    name = f"selftest-{int(time.time())}.radar"
    st.begin(name)
    st.write(b"RADAR2\n" + os.urandom(PART + 100))          # two parts: one full, one small
    st.finish({"patches": 1, "mapped": 1, "seconds": 2, "bytes": PART + 107})
    st.close()
    L = st.listing()
    assert name in L and L[name]["patches"] == 1, L
    import urllib.request
    assert len(urllib.request.urlopen(st.url(name)).read()) == PART + 107
    st.delete(name)
    assert name not in st.listing()
    print("store ok")
