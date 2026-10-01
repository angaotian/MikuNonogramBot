# -*- coding: utf-8 -*-
"""一条命令：把工作区的真源同步进 MikuPanel.exe，并自证没写坏。

    真源                              exe 内条目
    miku_logic_paint_bot.py      ->  miku_logic_paint_bot.py
    miku_bot_config.json         ->  miku_bot_config.json
    panel/index.html             ->  web\\index.html

流程：比对 -> 只改有差异的条目 -> 备份旧 exe -> 全量体检（252 个条目）-> 落盘。
任何一步不通过都不会覆盖原 exe（产物先写成 .new，体检过了才替换）。

用法：
    python tools/panel_build.py             # 有差异才动手
    python tools/panel_build.py --dry-run   # 只看差异，不写
    python tools/panel_build.py --force     # 内容一致也重新写一遍
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from panel_archive import EXE, ROOT, PanelArchive, audit, targets  # noqa: E402

BACKUP_DIR = ROOT / "build" / "versions"


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()[:16]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把真源同步进 MikuPanel.exe")
    ap.add_argument("--dry-run", action="store_true", help="只比对，不写文件")
    ap.add_argument("--force", action="store_true", help="内容一致也重新写一遍")
    args = ap.parse_args(argv)

    if not EXE.exists():
        print(f"[错误] 找不到面板程序：{EXE}")
        return 2
    arch = PanelArchive(EXE)

    print("=" * 66)
    print("MikuPanel.exe 打包（真源 -> exe）")
    print("=" * 66)

    updates = {}
    rows = []
    for tgt in targets():
        if not tgt.src.exists():
            print(f"[错误] 真源缺失：{tgt.src}")
            return 2
        cmp = arch.compare(tgt)
        rows.append(cmp)
        status = "一致" if cmp["same"] else "有差异"
        if not cmp["same"] or args.force:
            updates[cmp["entry"]] = tgt.src.read_bytes()
        print(f"  {tgt.key:<4} {cmp['entry']:<28} exe={md5(cmp['exe_bytes'])} "
              f"源={md5(cmp['src_bytes'])}  {status}")

    if not updates:
        print("\n已经一致，exe 不需要动。")
        return 0
    if args.dry_run:
        print(f"\n[dry-run] 需要更新 {len(updates)} 个条目，未写文件。")
        return 1

    print(f"\n改动的条目：{', '.join(updates)}")
    new_bytes, changed = arch.rebuild(updates)
    if not changed:
        print("没有实际变化。")
        return 0

    tmp = Path(str(EXE) + ".new")
    try:
        tmp.write_bytes(new_bytes)
    except OSError as exc:
        print(f"[错误] 写不出 {tmp}：{exc}\n（面板正在运行？先关掉 MikuPanel 窗口再试）")
        return 2
    try:
        audit(EXE, updates, log=print)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        print(f"[错误] 体检不通过，已丢弃产物，原 exe 未改：{exc}")
        return 3

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"MikuPanel-{stamp}-改动前.exe"
    try:
        shutil.copy2(EXE, backup)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        print(f"[错误] 备份失败，已放弃替换：{exc}")
        return 3
    print(f"  旧版已备份：{backup.relative_to(ROOT)}")

    try:
        os.replace(tmp, EXE)          # 同盘原子替换
    except OSError as exc:
        print(f"[错误] 替换失败（面板正在运行？）：{exc}\n产物留在 {tmp}")
        return 2

    print(f"\n完成：{EXE.name} 已更新（{len(changed)} 个条目，"
          f"{EXE.stat().st_size} 字节）")
    print("用 python tools/panel_verify.py 复核，或直接双击 启动面板.bat。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
