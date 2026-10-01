#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从工程生成「可发布」的两件套：运行包（含 exe）+ 源码树，并打包成 zip。

用法：
  python tools/make_release.py                # 用脚本里的 VERSION
  python tools/make_release.py --version 1.1.0
  python tools/make_release.py --no-zip       # 只生成目录，不压缩
  python tools/make_release.py --no-git       # 不执行 git init/commit

每次执行都会**重建** dist/ 下的同名目录（旧内容先删），所以可以反复跑；
真源只有三份（主脚本 / 配置 / panel/index.html），其它都是从真源派生的拷贝。

不发布的东西：debug/（含游戏截图与个人桌面现场）、build/（exe 历史备份）、
.pylibs/（打包环境）、.backup/、以及面板外壳的反汇编 dump（那是本地查证用的）。
"""
from __future__ import annotations

import argparse
import os
import re
import stat
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.0.0"
NAME = "MikuNonogramBot"
TITLE = "Miku 数织自动闯关（Hatsune Miku Logic Paint S 辅助工具）"

# 两件套都带的文件
COMMON_FILES = [
    "miku_logic_paint_bot.py",
    "miku_bot_config.json",
    "requirements.txt",
    "启动.bat",
    "启动面板.bat",
    "panel/index.html",
    "panel/shell/README.md",
    "tools/panel_archive.py",
    "tools/panel_build.py",
    "tools/panel_extract.py",
    "tools/panel_shell_disasm.py",
    "tools/panel_verify.py",
    "tools/make_release.py",
    "tools/gh_release.py",
]
# 只有运行包带（别人下载解压即可用）
RUN_FILES = ["MikuPanel.exe"]

GITIGNORE = """# 运行/排障产物（不入库）
__pycache__/
*.pyc
debug/
build/
dist/
.pylibs/
.backup/
MikuPanel.exe
MikuPanel.exe.new
"""

README = """# {title}

Windows 上给《Hatsune Miku Logic Paint S》里那套数织（nonogram / 数方）小游戏做的**自动闯关工具**。
它看着游戏窗口截图，自己认提示数字、求解、点格子、过关、再点下一关；带一个本地面板可以点按钮，
也可以纯命令行跑。

> 非官方项目，与游戏发行商/开发商无关。仅供个人学习与自用，请自行遵守游戏的服务条款。

## 它能做什么

- **自动读题**：截图 → OCR 识别行/列提示数字 → 交叉校验（行和=列和、可解、三帧一致）才开打
- **自动解题**：完整的数织求解器（含唯一解判定的多种尺寸 5×5 ~ 25×25）
- **自动填格**：分批填色 + 用「游戏自己的提示变灰」当判官，每批自查；空格按需打叉
- **自动连打**：过关后自动回列表、点下一关；读不通的关卡记进跳过表，先去打别的
- **两种关卡列表都会认**：普通谜题（浅黄卡片）与**特别谜题**（5×5 灰「?」方块阵）自动识别切换
- **紧急停止**：F8 / Esc / 把鼠标甩到屏幕左上角

## 下载即用（推荐，不需要装 Python）

1. 到本仓库的 **Releases** 页面下载 `{name}-<版本>-win64.zip`
2. 解压到任意目录
3. 双击 **`启动面板.bat`** → 面板窗口打开 → 把游戏停在关卡列表或棋盘上 → 点「连续闯关」

运行包自带 `MikuPanel.exe`（面板 GUI）与主脚本，面板启动时会自检、必要时自动把三份真源同步进 exe。
**仍然需要**：Windows + WebView2 运行时（Win11 自带）+ 本机装好 **Tesseract-OCR**（读数字用，见下）。

## 从源码跑（想改代码/命令行用这个）

```bat
pip install -r requirements.txt
python miku_logic_paint_bot.py --selftest     :: 不需要游戏的自检，先确认环境 OK
python miku_logic_paint_bot.py --loop         :: 开始连续闯关（游戏窗口保持在前台）
python miku_logic_paint_bot.py --dry-run      :: 只识别+求解，不点游戏
```

环境要求：

| 依赖 | 说明 |
|---|---|
| Python 3.10+ | 主脚本 |
| Pillow / numpy / pytesseract | `pip install -r requirements.txt` |
| **Tesseract-OCR** | 装在 `C:\\\\Program Files\\\\Tesseract-OCR\\\\` 或加入 PATH（脚本会自动找） |
| WebView2 运行时 | 只有面板 GUI 需要；命令行不需要 |

## 命令行参数

```
--loop [N]              连续闯关（N 为关数，省略表示一直打）
--dry-run               只识别+求解，不点击
--calibrate [--manual]  重新校准棋盘位置与尺寸（默认自动定位）
--probe                 诊断当前画面并保存标注截图
--goto R,C              把鼠标移到指定格子中心（0,0 = 左上角）后退出
--selftest              不需要游戏的自检（求解器 + 定位 + 列表布局 + 端到端）
--confirm               每次填格前按 Enter 确认
--mark-empty / --no-mark-empty   空隙要不要右键打叉（默认打）
--list-mode {{auto,normal,special}}   关卡列表布局：默认 auto 自动识别
--window WINDOW         自定义窗口标题关键字（逗号可多个）
--config CONFIG         指定配置文件路径
--hide-console          启动后最小化控制台
```

## 配置（`miku_bot_config.json`）

| 键 | 默认 | 作用 |
|---|---|---|
| `window_keywords` | Logic Paint / Hatsune / Miku | 认游戏窗口用的标题关键字 |
| `click_interval_ms` | 10 | 两下点击之间等多久（游戏卡就调大到 80~120） |
| `click_hold_ms` | 16 | 单次点击按下到抬起的毫秒数 |
| `fills_per_pause` | 24 | 每涂多少格做一次「提示变灰」自查 |
| `max_read_attempts` | 3 | 同一关最多读几次题 |
| `ocr_workers` | 8 | OCR 线程数（读数偶发不稳就设 1） |
| `list_mode` | auto | 关卡列表布局：auto / normal / special |
| `geom` | 自动写入 | 棋盘位置与格宽（`--calibrate` 或首次运行时自动落盘） |

## 怎么确认它认对了

- 跑起来日志里会出现 `[布局] 当前画面：特别谜题（5×5 灰「?」方块阵）` / `普通谜题（浅黄卡片）`——
  **布局一变就报一行**，这是「它知道自己在打哪种关卡」的直接证据。
- 面板日志里 `已回到关卡列表（特别谜题方块阵），点开这一格…` 与 `已回到关卡列表，点开这一关…`
  分别对应两种模式。
- 命令行 `python debug\\probe_special_list.py --live`（那是开发用的探针，**不随发布包提供**，只在本项目的开发目录里）
  可以只读地看一眼：布局判定、25 格逐格判定、下一步会点哪一格（不点击）。

## 已知限制（写清楚，别踩）

- **OCR 偶发读错**：个别关卡的小字号提示数字会被读错（表现为某一行/列始终不变灰）；
  脚本会三帧校验 + 纠错，还读不通就跳过这关去打别的，不会乱涂。
- **屏幕/窗口**：默认按客户区尺寸自适应（1280×720 / 1920×1080 实测），其它分辨率靠 `--calibrate` 自动定位；
  游戏窗口要能保持在前台，被遮挡会读错。
- **面板外壳不在源码里**：`MikuPanel.exe` 是一个 PyInstaller 打包的二进制（pywebview 外壳），
  本仓库只提供成品 exe 与「原地补丁式」打包工具 `tools/panel_build.py`（它需要一个现成 exe 当底座）。
  `panel/index.html` + 主脚本 + 配置是三份真源，改完用 `tools/panel_build.py` 同步进 exe。
- **仅 Windows**：窗口枚举、DPI、点击注入都用的 Win32 API。
- 游戏更新可能改界面，届时需要重新量坐标（`debug/` 里的探针是干这个用的，不随发布包提供）。

## 项目结构

```
miku_logic_paint_bot.py      主脚本（读题 / 求解 / 点击 / 连打，唯一的功能真源）
miku_bot_config.json         运行配置
panel/index.html             面板界面（改它必须重打包进 exe）
panel/shell/README.md        面板外壳（二进制）的行为记录与三条硬约束
tools/panel_build.py         把三份真源同步进 exe（原地补丁 + 全量体检 + 原子替换）
tools/panel_verify.py        体检：exe 内容物 vs 真源 / 副本 / 运行环境
tools/panel_extract.py       从 exe 里解出三份真源（对照用）
tools/panel_archive.py       PyInstaller CArchive 读写（供上面几个工具用）
tools/panel_shell_disasm.py  外壳字节码反汇编导出
启动面板.bat / 启动.bat        图形入口 / 命令行入口
MikuPanel.exe                预编译面板（Release 包提供）
docs/开发记录.md              开发记录：每条判据、每次踩坑与验证方式
```

## 开发与打包

打包/校验工具（`tools/panel_*.py`）要读写 exe 里的 PyInstaller CArchive，**额外需要 PyInstaller**：

```bat
pip install pyinstaller
python tools\\\\panel_verify.py     :: 先看 exe 与真源是否一致（内容物 / 副本 / 运行环境）
python tools\\\\panel_build.py      :: 打包（写 .new → 全量体检 → 备份旧版 → 原子替换）
python miku_logic_paint_bot.py --selftest
```

`panel_build.py` 是**原地补丁式**打包：它需要一个现成的 `MikuPanel.exe` 当底座（面板外壳的源码不在本仓库），
只改内容变了的条目，写完先落成 `MikuPanel.exe.new` 并做全量体检
（253 个条目全部可读、未改条目字节级一致），通过才替换；任何一步失败原 exe 一字节不动。

## 免责声明

本项目与《Hatsune Miku Logic Paint S》的发行商、开发商无任何关系，也不包含游戏的任何素材。
它是一个「看屏幕 + 点鼠标」的自动化脚本，作者不保证适用于任何特定用途；
使用前请自行确认不违反你所处地区的法律与游戏的服务条款，风险自负。

## 许可

MIT License（见 `LICENSE`）。游戏名称与相关商标归其各自权利人所有。
"""

LICENSE = """MIT License

Copyright (c) {year} {name} contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

CHANGELOG = """# 更新记录

## v{version}（首次公开发布）

- 自动读题：截图 + OCR + 行/列交叉校验（行和=列和、可解、三帧一致），支持 5×5 ~ 25×25
- 自动解题与填格：分批填色，每批用「游戏自己把提示变灰」当判官自查；空隙可自动打叉
- 自动连打：过关自动回列表并继续；读不通的关卡记入跳过表、先打别的（跳过前先确认盘面干净）
- **两种关卡列表自动识别**：普通谜题（浅黄卡片）与特别谜题（5×5 灰「?」方块阵），
  识别判据全部来自画面本身（格线、灰挡板、黄底占比、有没有棋盘），认不出就一个字都不点
- 面板 GUI：本地点按钮启动/停止、实时日志、模式与参数勾选
- 紧急停止：F8 / Esc / 鼠标甩到屏幕左上角
- 安全闸门：点击前确认「眼前是棋盘」、盲点不落在棋盘上、过关后走菜单退出而不是乱点
"""

RELEASE_NOTES = """## 下载

| 文件 | 给谁 |
|---|---|
| `{name}-{version}-win64.zip` | **想直接用**：解压 → 双击 `启动面板.bat`。自带面板 exe，不用装 Python |
| `{name}-{version}-src.zip` | **想改代码**：源码树，`pip install -r requirements.txt` 后用 Python 跑 |

两者都还需要本机装 **Tesseract-OCR**（读提示数字用）与 **WebView2**（只有面板 GUI 需要，Win11 自带）。

## 亮点

- 自动读题 + 求解 + 填格 + 过关连打，含**特别谜题（拼图方块阵）**模式的自动识别与切换
- 每批填色都用「游戏自己把提示变灰」当判官自查，发现读数有问题就停手，不硬涂
- 认不出界面时**一个字都不点**（宁可停着，也不把棋盘涂花）
- 读不通的关卡自动跳过、改打别的；跳过前先确认这一关盘面是干净的

## 快速验证（不用开游戏）

```bat
python miku_logic_paint_bot.py --selftest
```

求解器压测 → 棋盘定位 → 关卡列表布局 → 合成画面端到端，四段全过即可开打。

## 已知限制

个别关卡的小字号提示数字会被 OCR 读错（游戏侧表现是某行/列始终不变灰）。
脚本会三帧校验 + 纠错，仍读不通就跳过该关去打别的，不会乱涂。详见 README 的「已知限制」。
"""


def sanitize(text: str) -> str:
    """把开发记录里的本机绝对路径换掉（发布包不该带个人目录结构）。"""
    text = re.sub(r"[A-Za-z]:\\\\Users\\\\[^\\\\\s\)\"'`]+", "<用户目录>", text)
    text = text.replace(str(ROOT).replace("\\", "\\\\"), "<工程目录>")
    text = text.replace(str(ROOT), "<工程目录>")
    return text


def rmtree_force(p: Path) -> None:
    """删目录/文件：git 的 .git/objects 带只读属性，Windows 上直接 rmtree 会 WinError 5。"""
    def _onerr(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass
    if p.is_dir():
        shutil.rmtree(p, onerror=_onerr)
    elif p.exists():
        try:
            os.chmod(p, stat.S_IWRITE)
        except Exception:
            pass
        p.unlink()


def copy_into(dst_root: Path, rel: str) -> None:
    src = ROOT / rel
    if not src.exists():
        print("  [跳过] 源文件不存在：%s" % rel)
        return
    dst = dst_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def build_run_package(dst: Path, version: str) -> None:
    if dst.exists():
        rmtree_force(dst)
    for rel in COMMON_FILES + RUN_FILES:
        copy_into(dst, rel)
    write_common_docs(dst, version)
    print("  运行包：%s（%.1f MB）" % (dst, dir_size(dst) / 1048576))


def build_src_package(dst: Path, version: str, do_git: bool) -> None:
    if dst.exists():
        rmtree_force(dst)
    for rel in COMMON_FILES:
        copy_into(dst, rel)
    write_common_docs(dst, version)
    (dst / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    if do_git:
        try:
            subprocess.run(["git", "init", "-q"], cwd=str(dst), check=True)
            subprocess.run(["git", "add", "-A"], cwd=str(dst), check=True)
            subprocess.run(["git", "-c", "user.name=release", "-c", "user.email=release@local",
                            "commit", "-q", "-m", "release: v%s" % version],
                           cwd=str(dst), check=True)
            print("  源码树：%s（已 git init + 首次提交，等 remote + push）" % dst)
        except Exception as exc:
            print("  [提示] git 步骤没跑成（%s），目录已经生成，可自己 git init" % exc)
    else:
        print("  源码树：%s" % dst)


def write_common_docs(dst: Path, version: str) -> None:
    (dst / "README.md").write_text(README.format(title=TITLE, name=NAME), encoding="utf-8")
    (dst / "LICENSE").write_text(LICENSE.replace("{year}", "2026").replace("{name}", NAME),
                                 encoding="utf-8")
    (dst / "CHANGELOG.md").write_text(CHANGELOG.format(version=version), encoding="utf-8")
    docs = dst / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "发布说明.md").write_text(RELEASE_NOTES.format(name=NAME, version=version),
                                      encoding="utf-8")
    dev = ROOT / "README.md"
    if dev.exists():
        head = ("<!-- 这是开发记录，随源码一起发布只为透明。里面提到的 debug/ 现场、\n"
                "     build/versions 快照、探针输出等**不随仓库发布**，只有开发机上有。 -->\n\n")
        (docs / "开发记录.md").write_text(head + sanitize(dev.read_text(encoding="utf-8")),
                                          encoding="utf-8")


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def clean_pycache(p: Path) -> int:
    """把包里的 __pycache__/*.pyc 清掉（跑过一遍自检/校验就会长出来，别打进 zip）。"""
    n = 0
    for d in list(p.rglob("__pycache__")):
        rmtree_force(d)
        n += 1
    for f in list(p.rglob("*.pyc")):
        rmtree_force(f)
        n += 1
    return n


def make_zip(src: Path, out: Path) -> None:
    if out.exists():
        rmtree_force(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                z.write(f, arcname=str(Path(src.name) / f.relative_to(src)))
    print("  压缩包：%s（%.1f MB）" % (out, out.stat().st_size / 1048576))


GUIDE = """# 发布指南（GitHub）

生成物都在 `dist/`：

| 路径 | 用途 |
|---|---|
| `{name}-{version}-win64.zip` | **Release 下载件**：别人解压双击 `启动面板.bat` 就能用（含面板 exe） |
| `{name}-{version}-src.zip` | 源码压缩包（可不上传，仓库里就是这份源码） |
| `{name}-{version}/` | 运行包目录（zip 的内容） |
| `{name}-{version}-src/` | 源码树，**已经 git init + 首次提交**，配好 remote 就能 push |

## 一、建仓库并推上去

在 GitHub 新建一个空仓库（不要勾 README/gitignore），然后：

```bat
cd dist\\{name}-{version}-src
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git branch -M main
git push -u origin main
```

（用 SSH 就把 remote 换成 `git@github.com:<用户名>/<仓库名>.git`。）

## 二、建 Release 并上传 zip

```bat
gh release create v{version} ^
  "dist\\{name}-{version}-win64.zip" ^
  "dist\\{name}-{version}-src.zip" ^
  --title "v{version}" --notes-file "dist\\{name}-{version}-src\\docs\\发布说明.md"
```

没装 `gh` 就在仓库页面 → Releases → Draft a new release → Tag 填 `v{version}` →
把两个 zip 拖进附件 → 说明直接粘贴 `docs/发布说明.md` 的内容。

## 三、仓库设置建议

- **About**：`Windows 下给《Hatsune Miku Logic Paint S》里的数织小游戏做的自动闯关工具（读题 + 求解 + 自动点击，含特别谜题模式）`
- **Topics**：`nonogram` `picross` `automation` `opencv` `tesseract` `windows` `python` `game-bot`
- 建议在仓库描述里保留「非官方、自用、请遵守游戏服务条款」这句，避免误会

## 四、发布前自检清单

```bat
cd dist\\{name}-{version}
python miku_logic_paint_bot.py --selftest    :: 四段全过（下载者也能跑这一条）
python tools\\panel_verify.py                :: 内容物：通过 / 副本：干净 / 环境：通过
                                             :: （这条要 PyInstaller：pip install pyinstaller）
```

- [ ] `--selftest` 通过（证明包里的脚本自洽、依赖齐）
- [ ] `panel_verify` 通过（证明包里的 exe 与包里的真源一致）
- [ ] 两个 zip 都已生成
- [ ] 没有把 `debug/`、`build/`、`.pylibs/`、`.backup/` 带进仓库（`.gitignore` 已经挡住）
- [ ] 仓库里没有个人路径（本次生成时已自动替换开发记录里的绝对路径）
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="生成可发布的运行包 + 源码树（+ zip）")
    ap.add_argument("--version", default=VERSION)
    ap.add_argument("--no-zip", action="store_true")
    ap.add_argument("--no-git", action="store_true")
    args = ap.parse_args()
    v = args.version

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    run_dir = dist / ("%s-%s" % (NAME, v))
    src_dir = dist / ("%s-%s-src" % (NAME, v))

    print("版本 %s → %s" % (v, dist))
    print("== 运行包（含 exe）==")
    build_run_package(run_dir, v)
    print("== 源码树 ==")
    build_src_package(src_dir, v, do_git=not args.no_git)
    if not args.no_zip:
        print("== 压缩 ==")
        clean_pycache(run_dir)
        clean_pycache(src_dir)
        make_zip(run_dir, dist / ("%s-%s-win64.zip" % (NAME, v)))
        make_zip(src_dir, dist / ("%s-%s-src.zip" % (NAME, v)))
    (dist / "发布指南.md").write_text(GUIDE.format(name=NAME, version=v), encoding="utf-8")
    print("== 发布指南 ==\n  %s" % (dist / "发布指南.md"))
    print()
    print("自检建议：")
    print("  cd \"%s\" && python miku_logic_paint_bot.py --selftest" % run_dir)
    print("  cd \"%s\" && python tools/panel_verify.py" % run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
