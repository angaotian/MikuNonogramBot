# -*- coding: utf-8 -*-
"""从 MikuPanel.exe 里解出三件套（主脚本/配置/界面），用于对照或应急取回。

只读，不改 exe。默认解到 build/extracted/ ，也可指定目录。

    python tools/panel_extract.py                 # 解到 build/extracted/
    python tools/panel_extract.py -o 某目录
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from panel_archive import EXE, ROOT, PanelArchive, targets  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="解出面板 exe 里的内容物")
    ap.add_argument("-o", "--out", default=str(ROOT / "build" / "extracted"),
                    help="输出目录（默认 build/extracted/）")
    args = ap.parse_args(argv)

    arch = PanelArchive(EXE)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"来源：{EXE}（{len(arch.entries)} 个条目）")
    for tgt in targets():
        entry = arch.find(tgt)
        data = arch.read(entry.name)
        dest = out / Path(tgt.entry.replace("\\", "/")).name
        dest.write_bytes(data)
        flag = ""
        if tgt.src.exists():
            flag = "（与工作区真源一致）" if tgt.src.read_bytes() == data else "（与工作区真源不同！）"
        print(f"  {entry.name:<28} -> {dest}  {len(data)} 字节 {flag}")
    print(f"\n注意事项：这里解出的是副本，真源永远是 "
          f"{', '.join(str(t.src.relative_to(ROOT)) for t in targets())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
