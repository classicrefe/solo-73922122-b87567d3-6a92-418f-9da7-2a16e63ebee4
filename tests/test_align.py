#!/usr/bin/env python3
"""对照核心（app/src/compare.py）的单元测试：不依赖服务，直接 python3 运行。

覆盖：
- 起录不同（头部/尾部独有）的确定定位
- 换播（中段两侧各自独有）的确定定位
- 相同内容反复出现 → 不得任选一处，须给出可能范围
- 重复块 + 缺失位置不可唯一确定
- 无共同片段
- 清单缺段 / 包内文件缺失 / 对照期间包被改写 → CompareError
"""
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "src"))
import compare  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL "), name, ("" if cond else f"  -> {detail}"))


def aln(a, b):
    return compare.align(list(a), list(b))


# ---------- 1. 起录不同：A 多录了头，B 多录了尾 ----------
r = aln("ttuvwx", "uvwxzz")
check("起录不同: 共同段全部确定", r["essential"] == [(2, 0), (3, 1), (4, 2), (5, 3)], str(r))
check("起录不同: A 头部确定独有", r["unique_a"] == [0, 1], str(r["unique_a"]))
check("起录不同: B 尾部确定独有", r["unique_b"] == [4, 5], str(r["unique_b"]))
check("起录不同: 无歧义区域", r["ambiguous_regions"] == [], str(r["ambiguous_regions"]))

# ---------- 2. 换播：中段两侧各自独有 ----------
r = aln("abQQcd", "abWWcd")
check("换播: 共同段确定且保序", r["essential"] == [(0, 0), (1, 1), (4, 4), (5, 5)], str(r))
check("换播: A 独有中段", r["unique_a"] == [2, 3], str(r["unique_a"]))
check("换播: B 独有中段", r["unique_b"] == [2, 3], str(r["unique_b"]))

# ---------- 3. 全部内容相同、长度不同：任何位置都不得定死 ----------
r = aln("xxxxx", "xxx")
check("重复内容: 共同段数=短包", r["lcs"] == 3, str(r["lcs"]))
check("重复内容: 没有确定配对", r["essential"] == [], str(r["essential"]))
check("重复内容: 没有确定独有（多出的2段位置不可定）",
      r["unique_a"] == [] and r["unique_b"] == [], str(r))
check("重复内容: 给出一个覆盖全部可能位置的范围",
      len(r["ambiguous_regions"]) == 1
      and r["ambiguous_regions"][0]["a_lo"] == 0 and r["ambiguous_regions"][0]["a_hi"] == 4
      and r["ambiguous_regions"][0]["b_lo"] == 0 and r["ambiguous_regions"][0]["b_hi"] == 2
      and r["ambiguous_regions"][0]["matched"] == 3,
      str(r["ambiguous_regions"]))

# ---------- 4. 重复块：唯一锚点确定，重复块范围待核对 ----------
# A = H [x y x y] T, B = H [x y] T —— 多出的一个 xy 块位置不可唯一确定
r = aln(["H", "x", "y", "x", "y", "T"], ["H", "x", "y", "T"])
check("重复块: 首尾锚点确定", r["essential"] == [(0, 0), (5, 3)], str(r["essential"]))
check("重复块: 重复内容无确定独有", r["unique_a"] == [] and r["unique_b"] == [], str(r))
check("重复块: 重复块整体列为可能范围",
      len(r["ambiguous_regions"]) == 1
      and r["ambiguous_regions"][0]["a_lo"] == 1 and r["ambiguous_regions"][0]["a_hi"] == 4
      and r["ambiguous_regions"][0]["b_lo"] == 1 and r["ambiguous_regions"][0]["b_hi"] == 2
      and r["ambiguous_regions"][0]["matched"] == 2,
      str(r["ambiguous_regions"]))

# ---------- 5. 无共同片段 ----------
r = aln("abc", "xyz")
check("无共同: lcs=0 且两侧全部确定独有",
      r["lcs"] == 0 and r["unique_a"] == [0, 1, 2] and r["unique_b"] == [0, 1, 2], str(r))

# ---------- 6. 缺失位置可唯一确定（内容均唯一时） ----------
r = aln("abcde", "abde")
check("缺一段: 共同段确定", r["essential"] == [(0, 0), (1, 1), (3, 2), (4, 3)], str(r))
check("缺一段: 缺失段确定独有", r["unique_a"] == [2] and r["unique_b"] == [], str(r))

# ---------- 7. 单侧重复 vs 双侧重复 ----------
# A 中 x 出现两次、B 中一次：一个 x 必能匹配，但匹配哪个位置不可定
r = aln(["p", "x", "q", "x", "r"], ["p", "x", "r"])
check("单侧重复: 锚点 p/r 确定", r["essential"] == [(0, 0), (4, 2)], str(r["essential"]))
check("单侧重复: 两个 x 都不算确定独有（q 才是）", r["unique_a"] == [2], str(r["unique_a"]))
check("单侧重复: x 的位置给范围",
      len(r["ambiguous_regions"]) == 1
      and r["ambiguous_regions"][0]["a_lo"] == 1 and r["ambiguous_regions"][0]["a_hi"] == 3
      and r["ambiguous_regions"][0]["matched"] == 1,
      str(r["ambiguous_regions"]))

# ---------- 8. 包读取校验 ----------
def make_zip(path, playlist, files):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("playlist.m3u8", playlist)
        for name, data in files.items():
            zf.writestr(name, data)


tmp = tempfile.mkdtemp(prefix="cmp_unit_")
PL = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n" \
     + "".join(f"#EXTINF:2.000,\nseg_{i}.ts\n" for i in range(3)) + "#EXT-X-ENDLIST\n"
good = os.path.join(tmp, "good.zip")
make_zip(good, PL, {f"seg_{i}.ts": f"data-{i}".encode() for i in range(3)})

pkg = compare.load_package(good)
check("读包: 按清单顺序取指纹", len(pkg["fingerprints"]) == 3
      and pkg["names"] == [f"seg_{i}.ts" for i in range(3)], str(pkg["names"]))

# 清单引用的片段在包内缺失
missing = os.path.join(tmp, "missing.zip")
make_zip(missing, PL, {"seg_0.ts": b"d0", "seg_1.ts": b"d1"})
try:
    compare.load_package(missing)
    check("清单缺段被拒", False, "no error")
except compare.CompareError as exc:
    check("清单缺段被拒", "缺失" in str(exc), str(exc))

# 非 ZIP 文件
bad = os.path.join(tmp, "bad.zip")
with open(bad, "wb") as fh:
    fh.write(b"not a zip")
try:
    compare.load_package(bad)
    check("坏包被拒", False, "no error")
except compare.CompareError as exc:
    check("坏包被拒", "ZIP" in str(exc), str(exc))

# 对照期间包被改写（测试钩子在首次 stat 后重写文件）
mut = os.path.join(tmp, "mut.zip")
make_zip(mut, PL, {f"seg_{i}.ts": f"data-{i}".encode() for i in range(3)})

def rewrite():
    make_zip(mut, PL, {f"seg_{i}.ts": f"other-{i}".encode() for i in range(3)})

compare.TEST_HOOKS["after_first_stat"][mut] = rewrite
try:
    compare.load_package(mut)
    check("对照期间改写被拒", False, "no error")
except compare.CompareError as exc:
    check("对照期间改写被拒", "改写" in str(exc), str(exc))

# 端到端：两份 ZIP 对照报告
other = os.path.join(tmp, "other.zip")
PL2 = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n" \
      + "".join(f"#EXTINF:2.000,\ns_{i}.ts\n" for i in range(3)) + "#EXT-X-ENDLIST\n"
make_zip(other, PL2, {"s_0.ts": b"data-1", "s_1.ts": b"data-2", "s_2.ts": b"tail"})
rep = compare.compare_packages(good, other)
check("报告: 共同块两侧序号", rep["common_blocks"] == [
    {"a_start": 1, "a_end": 2, "b_start": 0, "b_end": 1, "count": 2,
     "sha256": rep["common_blocks"][0]["sha256"],
     "a_names": ["seg_1.ts", "seg_2.ts"], "b_names": ["s_0.ts", "s_1.ts"]}],
    str(rep["common_blocks"]))
check("报告: 确定独有带文件名",
      [u["name"] for u in rep["unique_a"]] == ["seg_0.ts"]
      and [u["name"] for u in rep["unique_b"]] == ["s_2.ts"], str(rep["unique_a"]))

print("\n==== SUMMARY ====")
failed = [n for n, ok in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
