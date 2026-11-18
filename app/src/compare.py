"""对照两份录音包（ZIP）：按包内 M3U8 顺序读取 TS 片段，比对字节指纹，
找出共同片段与确定独有片段，并把无法唯一确定的位置标为待人工核对的范围。

设计要点：
- 匹配必须保留播放顺序 → 用最长公共子序列（LCS）刻画"共同内容"。
- 相同内容反复出现时，对齐往往不唯一。只有出现在**所有**最优对齐中的
  配对才算"确定共同"；只出现在部分最优对齐中的位置列入"可能范围"；
  不出现在任何最优对齐中的序号才算"确定独有"。绝不任选一处当结论。
- 清单缺段、包内文件缺失、包在对照期间被改写 → 抛 CompareError，
  调用方不得发布结果。

本模块不依赖 FastAPI，可用普通 Python 直接单测。
"""

import hashlib
import os
import zipfile
from array import array

# 规模上限：单包段数与两包段数乘积（DP 表约为 2*n*m 字节，25M 格约 50MB、
# 纯 Python 约 20 秒）。超出则拒绝对照并说明。
MAX_SEGMENTS_PER_PACKAGE = 20000
MAX_MATRIX_CELLS = 25_000_000

# 测试钩子（仅测试进程内使用）：{zip_path: callable}，在首次 stat 之后、
# 读取之前触发，用于模拟"对照期间包被改写"。
TEST_HOOKS = {"after_first_stat": {}}


class CompareError(Exception):
    """对照输入不合法或对照期间包被改写；出现该错误时不得发布结果。"""


def _stat_sig(path: str):
    st = os.stat(path)
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _parse_playlist_order(text: str):
    """宽松解析包内清单：按出现顺序取出片段引用（非 # 行）。
    只用于已完成录音包的对照，不做录制侧的严格子集校验。"""
    names = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        names.append(ln)
    if not names:
        raise CompareError("包内清单没有任何片段")
    return names


def _find_playlist(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    if "playlist.m3u8" in names:
        return "playlist.m3u8"
    cands = [n for n in names if n.lower().endswith((".m3u8", ".m3u"))]
    if not cands:
        raise CompareError("包内找不到 M3U8 清单")
    if len(cands) > 1:
        raise CompareError(f"包内清单不唯一: {cands}")
    return cands[0]


def load_package(zip_path: str) -> dict:
    """读取一份录音包：返回按清单顺序的片段名与字节指纹。

    任一校验失败都抛 CompareError：
    - 清单引用的片段在包内缺失（清单缺段/文件缺失）
    - ZIP 损坏 / 不是合法 ZIP / 片段 CRC 校验失败
    - 读取期间包文件被改写（前后 stat 签名不一致）
    """
    if not os.path.exists(zip_path):
        raise CompareError(f"包文件不存在: {zip_path}")
    sig_before = _stat_sig(zip_path)
    hook = TEST_HOOKS["after_first_stat"].get(zip_path)
    if hook is not None:
        TEST_HOOKS["after_first_stat"].pop(zip_path, None)
        hook()  # 测试：在此刻改写包文件

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise CompareError(f"不是合法的 ZIP 包: {exc}")
    with zf:
        playlist_name = _find_playlist(zf)
        try:
            playlist_text = zf.read(playlist_name).decode("utf-8", "replace")
        except zipfile.BadZipFile as exc:
            raise CompareError(f"清单读取失败（包损坏）: {exc}")
        refs = _parse_playlist_order(playlist_text)
        if len(refs) > MAX_SEGMENTS_PER_PACKAGE:
            raise CompareError(
                f"包内片段数 {len(refs)} 超过对照上限 {MAX_SEGMENTS_PER_PACKAGE}")

        base = os.path.dirname(playlist_name)
        entries = set(zf.namelist())
        fingerprints = []
        names = []
        for ref in refs:
            # 片段引用相对于清单所在目录解析；拒绝逃出包根的路径
            norm = os.path.normpath(os.path.join(base, ref)).replace(os.sep, "/")
            if norm.startswith("../") or norm == ".." or os.path.isabs(ref):
                raise CompareError(f"清单内片段路径非法: {ref}")
            if norm not in entries:
                raise CompareError(f"清单引用的片段在包内缺失: {ref}")
            try:
                data = zf.read(norm)  # read 内部校验 CRC
            except zipfile.BadZipFile as exc:
                raise CompareError(f"片段 {ref} 读取失败（包损坏）: {exc}")
            fingerprints.append(hashlib.sha256(data).hexdigest())
            names.append(norm)

        # 包内容签名：覆盖全部条目的目录信息 + 清单文本 + 有序指纹，
        # 随结果保存，便于事后核对当时对照的是哪一份内容。
        content_sig = hashlib.sha256()
        for info in zf.infolist():
            content_sig.update(info.filename.encode())
            content_sig.update(str(info.CRC).encode())
            content_sig.update(str(info.file_size).encode())
        content_sig.update(playlist_text.encode())
        for fp in fingerprints:
            content_sig.update(fp.encode())

    if _stat_sig(zip_path) != sig_before:
        raise CompareError("对照期间包文件被改写，结果作废")
    return {
        "zip_path": zip_path,
        "playlist": playlist_name,
        "names": names,
        "fingerprints": fingerprints,
        "content_sig": content_sig.hexdigest(),
        "zip_size": sig_before[2],
        "zip_mtime_ns": sig_before[3],
    }


def align(fp_a: list, fp_b: list) -> dict:
    """对两列指纹做保序对照。

    返回：
      lcs:               共同内容段数（最优对齐的配对数）
      essential:         [(i, j)] 确定共同配对（出现在所有最优对齐中），按顺序
      unique_a/unique_b: 确定独有的下标（不出现在任何最优对齐中）
      ambiguous_regions: 无法唯一确定的区域，给出两侧可能范围，供人工核对
    """
    n, m = len(fp_a), len(fp_b)
    if n * m > MAX_MATRIX_CELLS:
        raise CompareError(
            f"两包片段数乘积 {n}×{m} 超过对照上限 {MAX_MATRIX_CELLS}，包过大")

    # 正向 DP：f[i][j] = LCS(fp_a[:i], fp_b[:j]) 的长度
    f = [array("H", bytes(2 * (m + 1))) for _ in range(n + 1)]
    for i in range(1, n + 1):
        ai = fp_a[i - 1]
        fi, fp = f[i], f[i - 1]
        for j in range(1, m + 1):
            if ai == fp_b[j - 1]:
                fi[j] = fp[j - 1] + 1
            else:
                v = fp[j]
                if fi[j - 1] > v:
                    v = fi[j - 1]
                fi[j] = v
    total = f[n][m]

    # 反向逐行：g_next[j] = LCS(fp_a[i+1:], fp_b[j:])，与 f 联合判定每个
    # 候选配对是否属于某个最优对齐，以及它在对齐中的位次（rank）。
    pos_in_b = {}
    for j, v in enumerate(fp_b):
        pos_in_b.setdefault(v, []).append(j)

    # rank -> [pair_count, min_i, max_i, min_j, max_j, pair_if_unique]
    ranks = {}
    in_any_a = [False] * n
    in_any_b = [False] * m
    g_next = array("H", bytes(2 * (m + 1)))
    for i in range(n - 1, -1, -1):
        ai = fp_a[i]
        g_cur = array("H", bytes(2 * (m + 1)))
        gj = 0
        for j in range(m - 1, -1, -1):
            if ai == fp_b[j]:
                gj = g_next[j + 1] + 1
            else:
                v = g_next[j]
                if g_cur[j + 1] > v:
                    v = g_cur[j + 1]
                gj = v
            g_cur[j] = gj
        if total:
            fi = f[i]
            for j in pos_in_b.get(ai, ()):
                before = fi[j]
                if before + 1 + g_next[j + 1] == total:
                    rank = before + 1
                    in_any_a[i] = True
                    in_any_b[j] = True
                    rec = ranks.get(rank)
                    if rec is None:
                        ranks[rank] = [1, i, i, j, j, (i, j)]
                    else:
                        rec[0] += 1
                        rec[1] = min(rec[1], i)
                        rec[2] = max(rec[2], i)
                        rec[3] = min(rec[3], j)
                        rec[4] = max(rec[4], j)
                        rec[5] = None
        g_next = g_cur

    essential = []
    ambiguous = []
    for rank in range(1, total + 1):
        rec = ranks[rank]
        if rec[0] == 1:
            essential.append(rec[5])
        else:
            ambiguous.append((rank, rec[1], rec[2], rec[3], rec[4]))

    # 连续的歧义位次合并为一个区域，给出两侧可能范围
    regions = []
    for rank, a_lo, a_hi, b_lo, b_hi in ambiguous:
        if regions and rank == regions[-1]["rank_hi"] + 1:
            reg = regions[-1]
            reg["rank_hi"] = rank
            reg["a_lo"] = min(reg["a_lo"], a_lo)
            reg["a_hi"] = max(reg["a_hi"], a_hi)
            reg["b_lo"] = min(reg["b_lo"], b_lo)
            reg["b_hi"] = max(reg["b_hi"], b_hi)
        else:
            regions.append({
                "rank_lo": rank, "rank_hi": rank,
                "a_lo": a_lo, "a_hi": a_hi, "b_lo": b_lo, "b_hi": b_hi,
            })
    for reg in regions:
        reg["matched"] = reg["rank_hi"] - reg["rank_lo"] + 1
        del reg["rank_lo"], reg["rank_hi"]

    return {
        "lcs": total,
        "essential": essential,
        "unique_a": [i for i in range(n) if not in_any_a[i]],
        "unique_b": [j for j in range(m) if not in_any_b[j]],
        "ambiguous_regions": regions,
    }


def _group_pairs(pairs: list) -> list:
    """把连续的确定配对 (i+1, j+1) 合并为块，便于阅读。"""
    blocks = []
    for i, j in pairs:
        if blocks and i == blocks[-1]["a_end"] + 1 and j == blocks[-1]["b_end"] + 1:
            blocks[-1]["a_end"] = i
            blocks[-1]["b_end"] = j
            blocks[-1]["count"] += 1
        else:
            blocks.append({"a_start": i, "a_end": i, "b_start": j, "b_end": j, "count": 1})
    return blocks


def build_report(pkg_a: dict, pkg_b: dict, aln: dict) -> dict:
    """把对照结果整理成可发布的报告（下标均为包内清单顺序，0 起）。"""
    names_a, names_b = pkg_a["names"], pkg_b["names"]
    fps_a, fps_b = pkg_a["fingerprints"], pkg_b["fingerprints"]

    blocks = _group_pairs(aln["essential"])
    for blk in blocks:
        blk["sha256"] = fps_a[blk["a_start"]][:12]
        blk["a_names"] = [names_a[i] for i in range(blk["a_start"], blk["a_end"] + 1)]
        blk["b_names"] = [names_b[j] for j in range(blk["b_start"], blk["b_end"] + 1)]

    unique_a = [{"index": i, "name": names_a[i], "sha256": fps_a[i][:12]}
                for i in aln["unique_a"]]
    unique_b = [{"index": j, "name": names_b[j], "sha256": fps_b[j][:12]}
                for j in aln["unique_b"]]

    regions = []
    for reg in aln["ambiguous_regions"]:
        regions.append({
            **reg,
            "a_names": [names_a[i] for i in range(reg["a_lo"], reg["a_hi"] + 1)],
            "b_names": [names_b[j] for j in range(reg["b_lo"], reg["b_hi"] + 1)],
        })

    return {
        "status": "ok" if aln["lcs"] > 0 else "no_common",
        "common_count": aln["lcs"],
        "definite_pairs": len(aln["essential"]),
        "common_blocks": blocks,
        "unique_a": unique_a,
        "unique_b": unique_b,
        "ambiguous_regions": regions,
        "package_a": {
            "segments": len(names_a), "playlist": pkg_a["playlist"],
            "content_sig": pkg_a["content_sig"][:16],
            "zip_size": pkg_a["zip_size"],
        },
        "package_b": {
            "segments": len(names_b), "playlist": pkg_b["playlist"],
            "content_sig": pkg_b["content_sig"][:16],
            "zip_size": pkg_b["zip_size"],
        },
    }


def compare_packages(zip_a: str, zip_b: str) -> dict:
    """对照两份 ZIP 包；任何校验问题都抛 CompareError（不得发布结果）。"""
    pkg_a = load_package(zip_a)
    pkg_b = load_package(zip_b)
    aln = align(pkg_a["fingerprints"], pkg_b["fingerprints"])
    return build_report(pkg_a, pkg_b, aln)
