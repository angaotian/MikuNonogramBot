# -*- coding: utf-8 -*-
"""把 MikuPanel.exe 外壳（app.py / web_panel.py）的字节码反汇编出来存成文本。

外壳源码不在工程里（PyInstaller 只留字节码），反汇编是了解它行为的唯一途径，
也是将来万一要重写外壳时的依据。只读 exe，产物默认写 panel/shell/。

    python tools/panel_shell_disasm.py            # 只导 app 和 web_panel
    python tools/panel_shell_disasm.py --all      # 连 webview 等模块一起导
"""
from __future__ import annotations

import argparse
import dis
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from panel_archive import EXE, ROOT, PanelArchive  # noqa: E402

sys.path.insert(0, str(ROOT / ".pylibs"))
from PyInstaller.archive.readers import ZlibArchiveReader  # noqa: E402


def dump(code, out: Path) -> int:
    lines: list[str] = []

    def walk(co, depth=0):
        lines.append("#" * 4 + f" code {co.co_name} (line {co.co_firstlineno})")
        lines.append(dis.Bytecode(co).dis())
        for const in co.co_consts:
            if hasattr(const, "co_code"):
                walk(const, depth + 1)

    walk(code)
    out.write_text("\n".join(lines), encoding="utf-8")
    return len(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="反汇编面板外壳模块")
    ap.add_argument("--all", action="store_true", help="导出 PYZ 里的全部模块")
    ap.add_argument("-o", "--out", default=str(ROOT / "panel" / "shell"))
    args = ap.parse_args(argv)

    arch = PanelArchive(EXE)          # 顺便校验 exe 结构
    pyz_name = next((n for n in arch.entries if n.name.lower().endswith(".pyz")), None)
    if pyz_name is None:
        print("[错误] exe 里没有 PYZ 归档")
        return 2

    scratch = ROOT / "build" / "_pyz.pyz"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_bytes(arch.read(pyz_name.name))
    zr = ZlibArchiveReader(str(scratch))
    names = sorted(zr.toc.keys())
    want = names if args.all else [n for n in names if n in ("app", "web_panel")]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for mod in want:
        obj = zr.extract(mod)
        if not hasattr(obj, "co_code"):
            continue
        out = out_dir / f"{mod.replace('.', '_')}.dis.txt"
        n = dump(obj, out)
        print(f"  {mod:<24} -> {out.relative_to(ROOT)}（{n} 个代码对象）")
    scratch.unlink(missing_ok=True)
    print(f"\n共 {len(names)} 个打包模块，已导出 {len(want)} 个。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
