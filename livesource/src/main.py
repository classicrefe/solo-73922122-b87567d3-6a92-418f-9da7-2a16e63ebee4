"""Local fake live HLS source for testing the recorder.

Serves a sliding-window media playlist over a pre-generated MPEG-TS seed
segment. Query parameters on /live.m3u8 are propagated to segment URLs:

  delay_ms=N            sleep N ms before serving each segment (slow source)
  missing=7,9           return 404 for those sequence numbers (missing segments)
  jump_at=K&jump_by=J   once the clock reaches seq K, shift all sequence
                        numbers by J (window jumps over undiscovered numbers)
  uniq=1                make every segment's bytes unique (deterministic per
                        sequence number: seed bytes + sha256 suffix), so two
                        recordings of the same program can be aligned
                        segment-by-segment
  alt=12,13             serve alternate content (seed_alt) for those sequence
                        numbers — simulates a source that switched programmes
                        (换播) for a while; combined with uniq=1 the differing
                        segments are uniquely identifiable

/prog.m3u8?first=F&count=C[&uniq=1][&alt=...] serves a deterministic finite
program (with ENDLIST) — two recordings with overlapping [first, first+count)
ranges partially overlap, and `alt` marks the segments that differ.

Also serves invalid playlists (/master.m3u8, /enc.m3u8, /disc.m3u8) and a
finite /vod.m3u8 for validation tests.
"""

import asyncio
import hashlib
import os
import time

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, PlainTextResponse

WINDOW_SIZE = int(os.environ.get("WINDOW_SIZE", "5"))
SEGMENT_DURATION = float(os.environ.get("SEGMENT_DURATION", "2"))
SEED_TS = os.environ.get("SEED_TS", "/srv/seed.ts")
SEED_ALT_TS = os.environ.get("SEED_ALT_TS", "/srv/seed_alt.ts")

EPOCH = time.time()
app = FastAPI(title="fake live HLS source")

_seed_cache = {}


def _seed_bytes(path: str) -> bytes:
    data = _seed_cache.get(path)
    if data is None:
        with open(path, "rb") as fh:
            data = fh.read()
        _seed_cache[path] = data
    return data


def _uniq_bytes(path: str, seq: int) -> bytes:
    """Deterministic per-sequence content: seed bytes + sha256 suffix.
    The suffix is a whole number of 188-byte TS packets, so the segment
    stays decodable while differing from every other sequence."""
    key = (path, seq)
    data = _seed_cache.get(key)
    if data is None:
        if len(_seed_cache) > 4000:  # 缓存上限：丢弃可重算的 uniq 条目
            for k in [k for k in _seed_cache if isinstance(k, tuple)]:
                del _seed_cache[k]
        suffix = (hashlib.sha256(f"{path}#{seq}".encode()).digest() * 12)[:376]
        data = _seed_bytes(path) + suffix
        _seed_cache[key] = data
    return data


def _now_seq() -> int:
    return int((time.time() - EPOCH) // SEGMENT_DURATION) + 1


def _parse_missing(missing: str) -> set:
    out = set()
    for part in (missing or "").split(","):
        part = part.strip()
        if part:
            out.add(int(part))
    return out


def _seg_query(delay_ms: int, missing: str, uniq: int, alt: str) -> str:
    return f"?delay_ms={delay_ms}&missing={missing}&uniq={uniq}&alt={alt}"


def _playlist(seqs, extra_lines=None, endlist=False) -> str:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{max(1, round(SEGMENT_DURATION))}",
        f"#EXT-X-MEDIA-SEQUENCE:{seqs[0]}",
    ]
    if extra_lines:
        lines.extend(extra_lines)
    for s in seqs:
        lines.append(f"#EXTINF:{SEGMENT_DURATION:.3f},")
        lines.append(f"/seg/{s}.ts")
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


@app.get("/live.m3u8", response_class=PlainTextResponse)
def live(delay_ms: int = 0, missing: str = "", jump_at: int = 0, jump_by: int = 0,
         uniq: int = 0, alt: str = ""):
    cur = _now_seq()
    eff = cur + (jump_by if jump_at and cur >= jump_at else 0)
    first = max(1, eff - WINDOW_SIZE + 1)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{max(1, round(SEGMENT_DURATION))}",
        f"#EXT-X-MEDIA-SEQUENCE:{first}",
    ]
    q = _seg_query(delay_ms, missing, uniq, alt)
    for s in range(first, eff + 1):
        lines.append(f"#EXTINF:{SEGMENT_DURATION:.3f},")
        lines.append(f"/seg/{s}.ts{q}")
    return "\n".join(lines) + "\n"


@app.get("/prog.m3u8", response_class=PlainTextResponse)
def prog(first: int = 1, count: int = 8, uniq: int = 0, alt: str = ""):
    """Deterministic finite programme (ENDLIST): sequences first..first+count-1.
    Two recordings with overlapping ranges partially overlap; `alt` marks the
    segments whose content differs (换播 simulation)."""
    if count < 1 or count > 500 or first < 1:
        raise HTTPException(400, "bad first/count")
    q = _seg_query(0, "", uniq, alt)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{max(1, round(SEGMENT_DURATION))}",
        f"#EXT-X-MEDIA-SEQUENCE:{first}",
    ]
    for s in range(first, first + count):
        lines.append(f"#EXTINF:{SEGMENT_DURATION:.3f},")
        lines.append(f"/seg/{s}.ts{q}")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


@app.get("/seg/{seq}.ts")
async def seg(seq: int, delay_ms: int = 0, missing: str = "", uniq: int = 0, alt: str = ""):
    if seq in _parse_missing(missing):
        raise HTTPException(404, f"segment {seq} intentionally missing")
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000.0)
    path = SEED_ALT_TS if seq in _parse_missing(alt) else SEED_TS
    headers = {"Cache-Control": "no-store"}
    if uniq:
        return Response(_uniq_bytes(path, seq), media_type="video/mp2t", headers=headers)
    return FileResponse(path, media_type="video/mp2t", headers=headers)


@app.get("/vod.m3u8", response_class=PlainTextResponse)
def vod():
    return _playlist([1, 2, 3], endlist=True)


@app.get("/reuse.m3u8", response_class=PlainTextResponse)
def reuse():
    # invalid: every sequence maps to the same segment URL (address reuse)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{max(1, round(SEGMENT_DURATION))}",
        "#EXT-X-MEDIA-SEQUENCE:1",
    ]
    for _ in range(3):
        lines.append(f"#EXTINF:{SEGMENT_DURATION:.3f},")
        lines.append("/seg/1.ts")
    return "\n".join(lines) + "\n"


@app.get("/master.m3u8", response_class=PlainTextResponse)
def master():
    return ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=320x240\n"
            "/live.m3u8\n")


@app.get("/enc.m3u8", response_class=PlainTextResponse)
def enc():
    return _playlist([1, 2, 3], extra_lines=[
        '#EXT-X-KEY:METHOD=AES-128,URI="/key.bin",IV=0x00000000000000000000000000000001'
    ])


@app.get("/disc.m3u8", response_class=PlainTextResponse)
def disc():
    seqs = [1, 2, 3]
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{max(1, round(SEGMENT_DURATION))}",
        "#EXT-X-MEDIA-SEQUENCE:1",
        "#EXTINF:2.000,",
        "/seg/1.ts",
        "#EXT-X-DISCONTINUITY",
        "#EXTINF:2.000,",
        "/seg/2.ts",
        "#EXTINF:2.000,",
        "/seg/3.ts",
    ]
    return "\n".join(lines) + "\n"


@app.get("/health")
def health():
    return {"ok": True, "now_seq": _now_seq()}


# Test-only hook (active solely when SOURCE_TEST_HOOKS=1): let test harnesses
# stop the process without signals.
if os.environ.get("SOURCE_TEST_HOOKS") == "1":
    @app.post("/api/_test/exit")
    def _test_exit():
        os._exit(0)


@app.get("/", response_class=PlainTextResponse)
def index():
    return (
        "fake live HLS source\n"
        "  /live.m3u8?delay_ms=0&missing=&jump_at=&jump_by=&uniq=0&alt=\n"
        "  /prog.m3u8?first=1&count=8&uniq=0&alt=   (deterministic finite programme)\n"
        "  /vod.m3u8 /master.m3u8 /enc.m3u8 /disc.m3u8\n"
    )
