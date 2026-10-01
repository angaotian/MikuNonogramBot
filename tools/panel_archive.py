# -*- coding: utf-8 -*-
"""面板包（MikuPanel.exe）低层读写库 —— 仅供 tools/ 下的脚本调用。

MikuPanel.exe 是 PyInstaller onefile：本体 PE 段 + 末尾一个 CArchive
（数据段 + TOC + cookie）。打包进去的三件内容物是：

    miku_logic_paint_bot.py    主脚本（面板优先加载 exe 同级的同名 .py）
    miku_bot_config.json       配置（面板用 base_dir/miku_bot_config.json）
    web\\index.html             界面（面板从 _MEIPASS 里取，所以只能打进 exe）

本库只做「原地追加 + 重写 TOC」：其它条目的偏移/长度一律不动，
新内容追加到数据段末尾，旧字节变成死区。这样不动 PE 段、不动任何依赖库，
改动面最小、可逆（改前备份）。
"""
from __future__ import annotations

import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
# 开发机上 PyInstaller 装在工程的 .pylibs/ 里（打包环境，不入库）；发布包里没有这一层，
# 所以这里两种都试：先 .pylibs/，再系统里 pip 装的那个。
if (ROOT / ".pylibs").is_dir():
    sys.path.insert(0, str(ROOT / ".pylibs"))

try:
    from PyInstaller.archive.readers import CArchiveReader  # noqa: E402
except ModuleNotFoundError:                                   # pragma: no cover
    print("需要 PyInstaller 才能读写 exe 里的 CArchive：")
    print("  pip install pyinstaller        （本机开发环境是放在工程的 .pylibs/ 里的）")
    raise SystemExit(2)

EXE = ROOT / "MikuPanel.exe"

COOKIE_FORMAT = "!8sIIII64s"
COOKIE_LEN = struct.calcsize(COOKIE_FORMAT)
TOC_FORMAT = "!IIIIBc"
TOC_LEN = struct.calcsize(TOC_FORMAT)


@dataclass(frozen=True)
class Target:
    """一个要打进 exe 的内容物：exe 内条目名 + 工作区里的真源文件。"""
    key: str
    entry: str
    src: Path

    @property
    def label(self) -> str:
        return f"{self.key} ({self.entry})"


def targets() -> List[Target]:
    return [
        Target("主脚本", "miku_logic_paint_bot.py", ROOT / "miku_logic_paint_bot.py"),
        Target("配置", "miku_bot_config.json", ROOT / "miku_bot_config.json"),
        Target("界面", "web\\index.html", ROOT / "panel" / "index.html"),
    ]


@dataclass
class Entry:
    name: str
    entry_length: int
    offset: int
    stored: int          # 数据段里占的字节数（压缩后）
    raw_len: int         # 解压后的字节数
    comp: int
    typecode: str
    raw: bytes           # TOC 里这一条的原始字节（原样搬运，避免精度损失）

    def tuple_of_ints(self) -> Tuple[int, int, int, int, str]:
        return (self.offset, self.stored, self.raw_len, self.comp, self.typecode)


class PanelArchive:
    """MikuPanel.exe 的 CArchive 读写封装。"""

    def __init__(self, exe: Path = EXE):
        self._load(Path(exe))

    @classmethod
    def open(cls, exe: Path) -> "PanelArchive":
        """按路径加载（等价于构造，供体检产物时用）。"""
        return cls(Path(exe))

    def _load(self, exe: Path) -> None:
        self.exe = exe
        if not self.exe.exists():
            raise FileNotFoundError(f"找不到面板程序：{self.exe}")
        self.raw = self.exe.read_bytes()
        self._reader = CArchiveReader(str(self.exe))
        self.start = self._reader._start_offset
        self.end = self._reader._end_offset
        if self.end != len(self.raw):
            raise RuntimeError("cookie 不在文件末尾，exe 结构异常，拒绝改动")
        cookie = self.raw[self.end - COOKIE_LEN:self.end]
        (self.magic, self.archive_length, self.toc_offset,
         self.toc_length, self.pyvers, self.pylib_name) = struct.unpack(COOKIE_FORMAT, cookie)
        if self.start + self.archive_length != self.end:
            raise RuntimeError("archive_length 与文件长度不一致，exe 结构异常，拒绝改动")

        self.toc_names = list(self._reader.toc.keys())
        self.entries: List[Entry] = self._parse_toc(
            self.raw[self.start + self.toc_offset:
                     self.start + self.toc_offset + self.toc_length])

    # ---------------- 读 ----------------
    @staticmethod
    def _parse_toc(toc_data: bytes) -> List[Entry]:
        entries: List[Entry] = []
        pos = 0
        while pos < len(toc_data):
            entry_length, offset, stored, raw_len, comp, typecode = struct.unpack(
                TOC_FORMAT, toc_data[pos:pos + TOC_LEN])
            raw_entry = toc_data[pos:pos + entry_length]
            name_len = entry_length - TOC_LEN
            name = toc_data[pos + TOC_LEN:pos + TOC_LEN + name_len].rstrip(b"\0").decode("utf-8")
            entries.append(Entry(name, entry_length, offset, stored, raw_len, comp,
                                 typecode.decode("ascii"), raw_entry))
            pos += entry_length
        return entries

    def read(self, name: str) -> bytes:
        """按条目名解出内容（自动解压）。"""
        if name not in self._reader.toc:
            raise KeyError(f"exe 里没有条目 {name!r}")
        return self._reader.extract(name)

    def find(self, target: Target) -> Entry:
        hit = [e for e in self.entries if e.name == target.entry]
        if not hit:                     # 兼容分隔符差异
            want = target.entry.replace("\\", "/").split("/")[-1]
            hit = [e for e in self.entries
                   if e.name.replace("\\", "/").split("/")[-1] == want]
        if len(hit) != 1:
            raise RuntimeError(f"条目 {target.entry!r} 不唯一（找到 {len(hit)} 个），拒绝改动")
        return hit[0]

    def compare(self, target: Target) -> Dict[str, object]:
        """把 exe 里的内容物和真源文件比一比。"""
        entry = self.find(target)
        src_bytes = target.src.read_bytes() if target.src.exists() else None
        exe_bytes = self.read(entry.name)
        return {
            "target": target,
            "entry": entry.name,
            "src": target.src,
            "src_bytes": src_bytes,
            "exe_bytes": exe_bytes,
            "size_src": len(src_bytes) if src_bytes is not None else None,
            "size_exe": len(exe_bytes),
            "same": src_bytes == exe_bytes,
        }

    # ---------------- 写 ----------------
    def rebuild(self, updates: Dict[str, bytes]) -> Tuple[bytes, List[str]]:
        """把 {条目名: 新内容} 打进 exe，返回 (新文件字节, 实际改动的条目名)。

        未列出的条目字节级不动；内容已一致的条目跳过。
        """
        data_region = self.raw[self.start:self.start + self.toc_offset]
        appended: List[bytes] = []
        new_offsets: Dict[str, Tuple[int, int, int]] = {}
        changed: List[str] = []

        for name, blob in updates.items():
            entry = [e for e in self.entries if e.name == name]
            if len(entry) != 1:
                raise RuntimeError(f"条目 {name!r} 不唯一（找到 {len(entry)} 个），拒绝改动")
            entry = entry[0]
            if self.read(name) == blob:
                continue                      # 已经一致，别白写
            stored = zlib.compress(blob, 9) if entry.comp else blob
            offset = self.toc_offset + sum(len(b) for b in appended)
            appended.append(stored)
            new_offsets[name] = (offset, len(stored), len(blob))
            changed.append(name)

        if not appended:
            return self.raw, []

        new_data_region = data_region + b"".join(appended)

        toc_parts: List[bytes] = []
        for entry in self.entries:
            if entry.name in new_offsets:
                offset, stored, raw_len = new_offsets[entry.name]
                nm = entry.name.encode("utf-8")
                name_len = len(nm) + 1
                entry_length = TOC_LEN + name_len
                if entry_length % 16 != 0:                 # 名字按 16 字节对齐补齐
                    name_len += 16 - (entry_length % 16)
                toc_parts.append(struct.pack(
                    TOC_FORMAT + "%ds" % name_len,
                    TOC_LEN + name_len, offset, stored, raw_len,
                    int(bool(entry.comp)), entry.typecode.encode("ascii"), nm))
            else:
                toc_parts.append(entry.raw)                # 其它条目原样搬运

        new_toc_data = b"".join(toc_parts)
        new_toc_offset = len(new_data_region)
        new_archive_length = new_toc_offset + len(new_toc_data) + COOKIE_LEN
        new_cookie = struct.pack(COOKIE_FORMAT, self.magic, new_archive_length,
                                 new_toc_offset, len(new_toc_data),
                                 self.pyvers, self.pylib_name)
        return self.raw[:self.start] + new_data_region + new_toc_data + new_cookie, changed


def audit(exe: Path, updates: Dict[str, bytes], log=print) -> None:
    """对「重写后的 exe」做全量体检：条目数、未改条目字节级一致、目标条目内容一致。"""
    before = PanelArchive(exe)
    produced = Path(str(exe) + ".new")
    New = PanelArchive.open(produced)

    if len(New.entries) != len(before.entries):
        raise AssertionError(f"条目数变了：{len(before.entries)} -> {len(New.entries)}")
    if set(New.toc_names) != set(before.toc_names):
        raise AssertionError("条目名集合变了")

    for old in before.entries:
        new = [e for e in New.entries if e.name == old.name][0]
        if old.name in updates:
            continue
        if new.tuple_of_ints() != old.tuple_of_ints() or len(new.raw) != len(old.raw):
            raise AssertionError(f"未改条目 {old.name!r} 的 TOC 被动了")
        if old.typecode == "o":
            continue          # OPTION 条目（如 pyi-contents-directory）没有内容可解
        if before.read(old.name) != New.read(old.name):
            raise AssertionError(f"未改条目 {old.name!r} 的内容被动了")
    for name, blob in updates.items():
        if New.read(name) != blob:
            raise AssertionError(f"目标条目 {name!r} 写进去的内容不对")
    untouched = len([e for e in before.entries if e.name not in updates and e.typecode != "o"])
    log(f"  体检通过：{len(New.entries)} 个条目全可读（其中 {untouched} 个未改条目字节级一致），"
        f"{len(updates)} 个目标条目内容与真源一致")
