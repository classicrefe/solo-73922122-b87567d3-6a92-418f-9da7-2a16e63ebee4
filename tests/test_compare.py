#!/usr/bin/env python3
"""End-to-end tests for recording comparison (对照).

Self-contained: starts its own fake source (deterministic /prog.m3u8
programmes) and the app, records programmes, then compares the ZIPs:

  P1 起录不同/部分重叠   uniq 内容 → 共同块与两侧独有全部确定
  P2 换播               alt 指定段不同 → 中段两侧各自确定独有
  P3 全部同内容         重复内容 → 无确定结论，只给可能范围
  P4 无共同片段         → 说明无法定位
  P5 包内文件缺失       → 不发布结果，原下载仍可用
  P6 对照期间包被改写   → 不发布结果，原下载仍可用
  P7 参数校验           同一份录音/未完成录音 → 400/409
"""
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.environ.get("APP_DIR", os.path.join(ROOT, "app"))
SRC_DIR = os.environ.get("SRC_DIR", os.path.join(ROOT, "livesource"))
FFMPEG = None
try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = free_port()
SRC_PORT = free_port()
APP = f"http://127.0.0.1:{PORT}"
SRC = f"http://127.0.0.1:{SRC_PORT}"
WORK = tempfile.mkdtemp(prefix="cmp_e2e_")
DATA = os.path.join(WORK, "data")
SEED_DIR = os.path.join(WORK, "seeds")
os.makedirs(SEED_DIR, exist_ok=True)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL "), name, ("" if cond else f"  -> {detail}"))


def make_seeds():
    """Real ffmpeg-generated TS seeds when possible; deterministic bytes otherwise."""
    seed = os.path.join(SEED_DIR, "seed.ts")
    alt = os.path.join(SEED_DIR, "seed_alt.ts")
    if FFMPEG:
        for path, freq in ((seed, 440), (alt, 880)):
            subprocess.run(
                [FFMPEG, "-y", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
                 "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=44100",
                 "-t", "2", "-c:v", "libx264", "-preset", "veryfast", "-g", "30",
                 "-c:a", "aac", "-f", "mpegts", path], check=True, timeout=60)
    else:
        with open(seed, "wb") as fh:
            fh.write(b"\x47" + b"SEED" * 5000)
        with open(alt, "wb") as fh:
            fh.write(b"\x47" + b"ALTS" * 5000)
    return seed, alt


SEED_TS, SEED_ALT_TS = make_seeds()

src_proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "src.main:app",
     "--host", "127.0.0.1", "--port", str(SRC_PORT)],
    cwd=SRC_DIR,
    env={**os.environ, "SEED_TS": SEED_TS, "SEED_ALT_TS": SEED_ALT_TS,
         "SOURCE_TEST_HOOKS": "1"},
    stdout=open(os.path.join(WORK, "src.log"), "wb"), stderr=subprocess.STDOUT)
app_log = open(os.path.join(WORK, "app.log"), "ab")
app_proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "src.main:app",
     "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=APP_DIR,
    env={**os.environ, "DATA_DIR": DATA, "RECORDER_TEST_HOOKS": "1"},
    stdout=app_log, stderr=subprocess.STDOUT)


def wait_up(url, proc, name):
    for _ in range(80):
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited during startup")
        try:
            httpx.get(url, timeout=2)
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"{name} did not start")


def api(method, path, **kw):
    return getattr(httpx, method)(f"{APP}{path}", timeout=15, **kw)


def record_prog(params):
    r = api("post", "/api/jobs", json={"url": f"{SRC}/prog.m3u8?{params}"})
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    for _ in range(100):
        j = api("get", f"/api/jobs/{jid}").json()
        if j["state"] == "done":
            return jid, j
        if j["state"] == "failed":
            raise RuntimeError(f"job {jid} failed: {j['error']}")
        time.sleep(0.3)
    raise RuntimeError(f"job {jid} not done in time")


def compare(ja, jb):
    r = api("post", "/api/comparisons", json={"job_a": ja, "job_b": jb})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    for _ in range(100):
        c = api("get", f"/api/comparisons/{cid}").json()
        if c["state"] != "running":
            return c
        time.sleep(0.3)
    raise RuntimeError(f"comparison {cid} still running")


try:
    wait_up(f"{SRC}/health", src_proc, "source")
    wait_up(f"{APP}/api/jobs", app_proc, "app")

    # ---------- 准备：录制确定性节目 ----------
    ja, _ = record_prog("first=1&count=8&uniq=1")          # seq 1..8，每段内容唯一
    jb, _ = record_prog("first=3&count=8&uniq=1")          # seq 3..10（与 A 部分重叠）
    jc, _ = record_prog("first=1&count=8&uniq=1&alt=5,6")  # 换播：seq 5,6 内容不同
    jd, _ = record_prog("first=1&count=5")                 # 全部同内容（重复）
    je, _ = record_prog("first=1&count=3")
    jf, _ = record_prog("first=1&count=3&uniq=1")
    jg, _ = record_prog("first=100&count=3&uniq=1")

    # ---------- P1: 起录不同（部分重叠），uniq 内容 → 全部确定 ----------
    c = compare(ja, jb)
    R = c["result"]
    check("P1 对照完成", c["state"] == "done" and R["status"] == "ok", str(c["state"]))
    check("P1 共同块两侧序号", [(b["a_start"], b["a_end"], b["b_start"], b["b_end"])
          for b in R["common_blocks"]] == [(2, 7, 0, 5)], str(R["common_blocks"]))
    check("P1 A 侧确定独有（序号+包内文件名）",
          [(u["index"], u["name"]) for u in R["unique_a"]]
          == [(0, "seg_00000001.ts"), (1, "seg_00000002.ts")], str(R["unique_a"]))
    check("P1 B 侧确定独有",
          [(u["index"], u["name"]) for u in R["unique_b"]]
          == [(6, "seg_00000009.ts"), (7, "seg_00000010.ts")], str(R["unique_b"]))
    check("P1 无待核对区域", R["ambiguous_regions"] == [], str(R["ambiguous_regions"]))

    # ---------- P2: 换播（alt 指定段不同） ----------
    c = compare(ja, jc)
    R = c["result"]
    check("P2 共同块（换播段前后）", [(b["a_start"], b["a_end"], b["b_start"], b["b_end"])
          for b in R["common_blocks"]] == [(0, 3, 0, 3), (6, 7, 6, 7)], str(R["common_blocks"]))
    check("P2 两侧各自确定独有换播段",
          [u["index"] for u in R["unique_a"]] == [4, 5]
          and [u["index"] for u in R["unique_b"]] == [4, 5]
          and R["unique_b"][0]["name"] == "seg_00000005.ts", str(R["unique_b"]))
    check("P2 无待核对区域", R["ambiguous_regions"] == [], str(R["ambiguous_regions"]))

    # ---------- P3: 全部同内容 → 不得任选一处 ----------
    c = compare(jd, je)
    R = c["result"]
    check("P3 共同段数=短包", R["common_count"] == 3, str(R["common_count"]))
    check("P3 无确定共同配对", R["definite_pairs"] == 0 and R["common_blocks"] == [], str(R))
    check("P3 无确定独有（多出的 2 段位置不可定）",
          R["unique_a"] == [] and R["unique_b"] == [], str(R))
    check("P3 给出可能范围供人工核对",
          len(R["ambiguous_regions"]) == 1
          and R["ambiguous_regions"][0]["a_lo"] == 0 and R["ambiguous_regions"][0]["a_hi"] == 4
          and R["ambiguous_regions"][0]["b_lo"] == 0 and R["ambiguous_regions"][0]["b_hi"] == 2
          and R["ambiguous_regions"][0]["matched"] == 3,
          str(R["ambiguous_regions"]))

    # ---------- P4: 无共同片段 → 说明无法定位 ----------
    c = compare(jf, jg)
    check("P4 无共同片段", c["state"] == "done" and c["result"]["status"] == "no_common"
          and c["result"]["common_count"] == 0, str(c["result"]["status"]))

    # ---------- P5: 包内文件缺失 → 不发布结果，原下载可用 ----------
    zip_a = os.path.join(DATA, "zips", f"job_{ja}.zip")
    bak = zip_a + ".bak"
    shutil.copy(zip_a, bak)
    with zipfile.ZipFile(zip_a) as zf:
        items = [(i.filename, zf.read(i.filename)) for i in zf.infolist()]
    victim = "seg_00000002.ts"
    with zipfile.ZipFile(zip_a, "w", zipfile.ZIP_STORED) as zf:
        for name, data in items:
            if name != victim:
                zf.writestr(name, data)
    c = compare(ja, jb)
    check("P5 清单缺段/文件缺失不发布结果",
          c["state"] == "failed" and "缺失" in (c["error"] or "") and c["result"] is None,
          str(c["error"]))
    r = api("get", f"/api/jobs/{ja}/download")
    check("P5 原下载继续可用", r.status_code == 200, f"got {r.status_code}")
    shutil.move(bak, zip_a)  # 恢复，供后续用例使用

    # ---------- P6: 对照期间包被改写 → 不发布结果 ----------
    r = api("post", f"/api/_test/arm_zip_rewrite/{jb}")
    assert r.status_code == 200, r.text
    c = compare(ja, jb)
    check("P6 对照期间改写不发布结果",
          c["state"] == "failed" and "改写" in (c["error"] or "") and c["result"] is None,
          str(c["error"]))
    r = api("get", f"/api/jobs/{jb}/download")
    check("P6 原下载继续可用", r.status_code == 200, f"got {r.status_code}")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    check("P6 改写后的包仍是合法 ZIP", "playlist.m3u8" in zf.namelist(), str(zf.namelist()[:3]))

    # ---------- P7: 参数校验 ----------
    r = api("post", "/api/comparisons", json={"job_a": ja, "job_b": ja})
    check("P7 同一份录音被拒(400)", r.status_code == 400, f"got {r.status_code}")
    r = api("post", "/api/jobs", json={"url": f"{SRC}/live.m3u8"})
    live_job = r.json()["id"]
    r = api("post", "/api/comparisons", json={"job_a": ja, "job_b": live_job})
    check("P7 未完成录音被拒(409)", r.status_code == 409, f"got {r.status_code}")
    api("post", f"/api/jobs/{live_job}/stop")
    r = api("get", "/api/comparisons")
    check("P7 对照列表可查", r.status_code == 200 and len(r.json()) >= 6, str(r.status_code))

    # ---------- P8: 重启时仍在 running 的对照不发布 ----------
    import sqlite3
    conn = sqlite3.connect(os.path.join(DATA, "recorder.db"))
    cur = conn.execute(
        "INSERT INTO comparisons(job_a, job_b, state, created_at) VALUES (?,?, 'running', ?)",
        (ja, jb, time.time()))
    stale_id = cur.lastrowid
    conn.commit()
    conn.close()
    try:
        api("post", "/api/_test/crash")      # 应用立即退出（连接被切断属预期）
    except Exception:
        pass
    app_proc.wait(timeout=10)
    time.sleep(0.5)
    app_proc2 = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=APP_DIR,
        env={**os.environ, "DATA_DIR": DATA, "RECORDER_TEST_HOOKS": "1"},
        stdout=app_log, stderr=subprocess.STDOUT)
    app_proc = app_proc2  # 模块级代码，直接替换供 finally 清理
    wait_up(f"{APP}/api/jobs", app_proc2, "app(restarted)")
    c = api("get", f"/api/comparisons/{stale_id}").json()
    check("P8 重启中断的对照不发布", c["state"] == "failed" and c["result"] is None
          and "重启" in (c["error"] or ""), str(c))
    c = api("get", f"/api/comparisons/1").json()
    check("P8 已发布的对照结果重启后仍可查", c["state"] == "done"
          and c["result"]["common_count"] == 6, str(c["state"]))

finally:
    # 本环境禁止跨进程发信号：用测试钩子让两个进程自行退出
    for url in (f"{APP}/api/_test/crash", f"{SRC}/api/_test/exit"):
        try:
            httpx.post(url, timeout=3)
        except Exception:
            pass
    for proc in (app_proc, src_proc):
        try:
            proc.wait(timeout=10)
        except Exception:
            pass

print("\n==== SUMMARY ====")
failed = [n for n, ok in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", *failed, sep="\n  - ")
sys.exit(1 if failed else 0)
