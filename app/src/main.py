import asyncio
import json
import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .compare import CompareError, TEST_HOOKS as COMPARE_TEST_HOOKS, compare_packages
from .db import DB
from .recorder import Manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

DATA_DIR = os.environ.get("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)

db = DB(os.path.join(DATA_DIR, "recorder.db"))
mgr = Manager(db, DATA_DIR)
app = FastAPI(title="HLS 直播连续录音采集")


@app.on_event("startup")
async def _startup():
    db.fail_stale_comparisons()  # comparisons interrupted by a restart stay unpublished
    mgr.recover()  # resume any recording/finalizing jobs after a restart


@app.on_event("shutdown")
async def _shutdown():
    await mgr.shutdown()


class StartBody(BaseModel):
    url: str


def job_json(job_id: int) -> dict:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    counts = db.counts(job_id)
    out = {
        "id": job["id"],
        "url": job["url"],
        "state": job["state"],
        "created_at": job["created_at"],
        "first_seq": job["first_seq"],
        "last_seq": job["last_seq"],
        "stop_seq": job["stop_seq"],
        "expected": counts["total"],
        "saved": counts["done"],
        "missing": db.missing_seqs(job_id),
        "error": job["error"],
        "download_url": f"/api/jobs/{job_id}/download" if job["state"] == "done" else None,
    }
    return out


@app.post("/api/jobs")
async def start_job(body: StartBody):
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "只支持 http(s) 清单地址")
    job_id = mgr.create_job(url)
    return job_json(job_id)


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: int):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job["state"] != "recording":
        raise HTTPException(409, f"当前状态 {job['state']} 不可停止")
    mgr.accept_stop(job_id)
    return job_json(job_id)


@app.get("/api/jobs")
async def list_jobs():
    return [job_json(j["id"]) for j in db.list_jobs()]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int):
    return job_json(job_id)


@app.get("/api/jobs/{job_id}/download")
async def download(job_id: int):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job["state"] != "done" or not job["zip_path"] or not os.path.exists(job["zip_path"]):
        raise HTTPException(409, "全部应收片段保存后才可下载")
    return FileResponse(job["zip_path"], media_type="application/zip",
                        filename=f"recording_{job_id}.zip")


# ---- 录音对照 ----

class CompareBody(BaseModel):
    job_a: int
    job_b: int


_background_tasks = set()  # 保持对照任务的强引用，避免被 GC


def cmp_json(row: dict) -> dict:
    out = {
        "id": row["id"],
        "job_a": row["job_a"],
        "job_b": row["job_b"],
        "state": row["state"],
        "created_at": row["created_at"],
        "error": row["error"],
        "result": json.loads(row["result"]) if row["result"] else None,
    }
    return out


async def _run_comparison(cmp_id: int, zip_a: str, zip_b: str):
    try:
        report = await asyncio.to_thread(compare_packages, zip_a, zip_b)
    except CompareError as exc:
        # 清单缺段 / 文件缺失 / 包被改写 / 包过大：不发布结果，原下载不受影响
        db.comparison_failed(cmp_id, str(exc))
        log.warning("comparison %s not published: %s", cmp_id, exc)
        return
    except Exception:
        log.exception("comparison %s crashed", cmp_id)
        db.comparison_failed(cmp_id, "对照内部错误")
        return
    db.comparison_done(cmp_id, json.dumps(report, ensure_ascii=False))
    log.info("comparison %s done: %s common segments", cmp_id, report["common_count"])


@app.post("/api/comparisons")
async def start_comparison(body: CompareBody):
    if body.job_a == body.job_b:
        raise HTTPException(400, "请选择两份不同的录音")
    zips = []
    for job_id in (body.job_a, body.job_b):
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(404, f"录音任务 {job_id} 不存在")
        if job["state"] != "done" or not job["zip_path"] or not os.path.exists(job["zip_path"]):
            raise HTTPException(409, f"录音 {job_id} 尚未完成，不能对照")
        zips.append(job["zip_path"])
    cmp_id = db.insert_comparison(body.job_a, body.job_b)
    task = asyncio.create_task(_run_comparison(cmp_id, zips[0], zips[1]))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return cmp_json(db.get_comparison(cmp_id))


@app.get("/api/comparisons")
async def list_comparisons():
    return [cmp_json(r) for r in db.list_comparisons()]


@app.get("/api/comparisons/{cmp_id}")
async def get_comparison(cmp_id: int):
    row = db.get_comparison(cmp_id)
    if row is None:
        raise HTTPException(404, "comparison not found")
    return cmp_json(row)


@app.get("/")
async def index():
    page = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(page, encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


# Test-only hooks for crash/recovery testing. Enabled exclusively via
# RECORDER_TEST_HOOKS=1; never set in the Compose deployment.
if os.environ.get("RECORDER_TEST_HOOKS") == "1":
    from .recorder import TEST_HOOKS

    @app.post("/api/_test/crash")
    async def _test_crash():
        os._exit(137)

    @app.post("/api/_test/arm_after_write/{job_id}/{seq}")
    async def _test_arm_after_write(job_id: int, seq: int):
        TEST_HOOKS["crash_after_write"] = (job_id, seq)
        return {"armed": [job_id, seq]}

    @app.post("/api/_test/arm_zip_rewrite/{job_id}")
    async def _test_arm_zip_rewrite(job_id: int):
        """测试钩子：下一次对照读取该任务的 ZIP 时，在首次 stat 之后重写包，
        模拟"对照期间包被改写"。"""
        job = db.get_job(job_id)
        if job is None or not job["zip_path"]:
            raise HTTPException(404, "job/zip not found")
        zip_path = job["zip_path"]

        def rewrite():
            import zipfile
            with zipfile.ZipFile(zip_path) as zf:
                items = [(i.filename, zf.read(i.filename)) for i in zf.infolist()]
            tmp = zip_path + ".rewrite"
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
                for name, data in items:
                    zf.writestr(name, data)
            os.replace(tmp, zip_path)  # 内容相同但 inode/mtime 已变

        COMPARE_TEST_HOOKS["after_first_stat"][zip_path] = rewrite
        return {"armed": zip_path}