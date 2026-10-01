# -*- coding: utf-8 -*-
"""只读校验：面板 exe 里的东西 == 工作区真源吗？环境齐不齐？

三块体检：
  ① 内容物比对    exe 里的 主脚本/配置/界面 与工作区真源逐个 md5 对照
  ② 副本巡检      项目里是否还有同一文件的其它副本（防止改错文件）
  ③ 运行环境      依赖 / Tesseract / 配置文件合法性 / 棋盘几何

退出码：0 = 全部通过；1 = 有内容物漂移；2 = 环境有问题（两类可同时出现，取 2 优先）。

用法：
    python tools/panel_verify.py            # 完整报告
    python tools/panel_verify.py --quiet    # 只报问题，供 .bat 调用
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from panel_archive import EXE, ROOT, PanelArchive, targets  # noqa: E402

SKIP_DIRS = {".pylibs", "__pycache__", "build", ".git"}


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()[:16]


def check_contents(arch: PanelArchive, quiet: bool) -> bool:
    if not quiet:
        print("① 内容物比对（exe 内 vs 工作区真源）")
        print(f"   {'项':<4} {'exe 内条目':<28} {'exe':<17} {'真源':<17} 结论")
    ok = True
    for tgt in targets():
        if not tgt.src.exists():
            print(f"  [缺失] 真源不存在：{tgt.src}")
            ok = False
            continue
        cmp = arch.compare(tgt)
        if not quiet or not cmp["same"]:
            print(f"   {tgt.key:<4} {cmp['entry']:<28} {md5(cmp['exe_bytes']):<17} "
                  f"{md5(cmp['src_bytes']):<17} {'一致' if cmp['same'] else '漂移！'}")
        if not cmp["same"]:
            ok = False
    return ok


def check_copies(quiet: bool) -> bool:
    """项目里除了真源之外，还有没有同名副本（有漂移的副本 = 容易改错文件）。

    debug/ 与 build/ 里的是排障留档/解包产物，只提示不判定；其它位置的副本一旦
    与真源不一致就算不合格——那才是"会改错文件"的地方。
    """
    names = {t.src.name: t.src for t in targets()}
    sources = [t.src.resolve() for t in targets()]
    found: list[tuple[Path, Path, bool, bool]] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.name not in names:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        real = path.resolve()
        if real in sources or path.suffix.lower() == ".exe":
            continue
        canon = names[path.name]
        try:
            same = path.read_bytes() == canon.read_bytes()
        except OSError:
            same = False
        scratch = path.parts[len(ROOT.parts)] in ("debug", "build")
        found.append((path, canon, same, scratch))

    if not quiet:
        print("② 副本巡检（除了真源，还有谁叫这个名字）")
    ok = True
    if not found:
        if not quiet:
            print("   无多余副本")
        return ok
    for path, canon, same, scratch in sorted(found):
        rel = path.relative_to(ROOT)
        if scratch:
            if not quiet:
                print(f"   {str(rel):<44} 留档副本"
                      f"（{'与真源一致' if same else '比真源旧'}，不参与判定）")
        elif same:
            if not quiet:
                print(f"   {str(rel):<44} 与真源一致（可随时重生成）")
        else:
            print(f"   {str(rel):<44} 与真源【不一致】← 别改这个文件")
            ok = False
    return ok


def check_env(quiet: bool) -> bool:
    if not quiet:
        print("③ 运行环境")
    problems = []

    for mod in ("PIL", "numpy", "pytesseract"):
        try:
            __import__(mod)
            if not quiet:
                print(f"   {mod:<12} OK")
        except Exception as exc:
            problems.append(f"缺库 {mod}（{exc}）—— 跑 pip install -r requirements.txt")
            print(f"   {mod:<12} 缺失：{exc}")

    cands = [shutil.which("tesseract"),
             r"C:\Program Files\Tesseract-OCR\tesseract.exe",
             r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
             str(Path.home() / "AppData/Local/Programs/Tesseract-OCR/tesseract.exe")]
    tess = next((c for c in cands if c and Path(c).exists()), None)
    if tess:
        if not quiet:
            print(f"   {'tesseract':<12} {tess}")
    else:
        problems.append("找不到 Tesseract-OCR（读不了提示数字）")
        print("   tesseract    找不到")

    cfg_path = ROOT / "miku_bot_config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        geom = cfg.get("geom")
        if not isinstance(geom, dict):
            problems.append("配置里没有棋盘几何数据（首次运行会自动定位，不算错）")
            if not quiet:
                print("   geom         待自动定位")
        else:
            rows, cols = geom.get("rows"), geom.get("cols")
            if rows != cols or rows not in (5, 10, 15, 20, 25):
                problems.append(f"配置里的棋盘尺寸异常：{rows}x{cols}")
                print(f"   geom         {rows}x{cols} 异常")
            elif not quiet:
                print(f"   geom         {rows}x{cols} OK")
        legacy = sorted(set(cfg) - {
            "window_keywords", "geom", "next_button", "click_hold_ms", "click_interval_ms",
            "fills_per_pause", "pause_ms", "max_read_attempts", "ocr_workers",
            "solver_time_limit", "paint_diff_threshold", "max_reclick_cells",
            "next_button_delay", "clear_wait_timeout", "auto_confirm", "failsafe_corner",
            "mark_empty", "list_mode"})
        if legacy and not quiet:
            print(f"   配置冗余键   {', '.join(legacy)}（主脚本会忽略，写了也不会被读）")
        if not quiet and cfg.get("stars"):
            print("   注意         配置里还留着已删除的星数记录（stars / check_stars），"
                  "主脚本已不读它们，下次保存会消失")
    except Exception as exc:
        problems.append(f"配置文件读不了：{exc}")
        print(f"   config       读不了：{exc}")

    if problems:
        for p in problems:
            print(f"   [问题] {p}")
        return False
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="面板与工作区一致性 + 环境校验")
    ap.add_argument("--quiet", action="store_true", help="只报问题")
    args = ap.parse_args(argv)

    try:
        arch = PanelArchive(EXE)
    except Exception as exc:
        print(f"[错误] 读不了 {EXE}：{exc}")
        return 2

    if not args.quiet:
        print("=" * 74)
        print(f"面板校验　{EXE.name}　{EXE.stat().st_size} 字节　{len(arch.entries)} 个条目")
        print("=" * 74)

    contents_ok = check_contents(arch, args.quiet)
    copies_ok = check_copies(args.quiet)
    env_ok = check_env(args.quiet)

    if args.quiet:
        if not contents_ok:
            print("[面板校验] exe 内有内容物与工作区源码不一致 -> 需要 tools/panel_build.py 重新打包")
        if not copies_ok:
            print("[面板校验] 项目里存在与真源不一致的同名副本，注意别改错文件")
        if not env_ok:
            print("[面板校验] 运行环境有问题（见上）")
        if contents_ok and copies_ok and env_ok:
            print("[面板校验] 全部通过")
    else:
        print("-" * 74)
        print(f"内容物：{'通过' if contents_ok else '有漂移'}"
              f"    副本：{'干净' if copies_ok else '有漂移副本'}"
              f"    环境：{'通过' if env_ok else '有问题'}")

    if not env_ok:
        return 2
    return 0 if (contents_ok and copies_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
