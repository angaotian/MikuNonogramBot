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
VERSION = "1.0.13"
NAME = "MikuNonogramBot"
OWNER = "angaotian"          # GitHub 用户名（README / 发布说明里的直链要用）
TITLE = "Miku 数织自动闯关（Hatsune Miku Logic Paint S 辅助工具）"

# 两件套都带的文件
COMMON_FILES = [
    "miku_logic_paint_bot.py",
    "miku_bot_config.json",
    "requirements.txt",
    "启动.bat",
    "启动面板.bat",
    "卸载.bat",
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

## ⬇️ 下载（Windows，免安装）

**[点这里直接下载 MikuNonogramBot-{version}-win64.zip](https://github.com/{owner}/{name}/releases/latest/download/{name}-{version}-win64.zip)**（约 32 MB，自带面板，不用装 Python）

- 想下最新版永远用这个地址：<https://github.com/{owner}/{name}/releases/latest>
- 只想看代码：[{name}-{version}-src.zip](https://github.com/{owner}/{name}/releases/latest/download/{name}-{version}-src.zip)
- 下载后：**解压 → 双击 `启动面板.bat` → 把游戏停在关卡列表或棋盘上 → 点「连续闯关」**

> 在 GitHub 网页上找不到下载位置的话：页面**右侧边栏**的 **Releases → v{version}**，或页面底部 **Releases** 区域，点进去最下面就是 **Assets**（两个 zip）。

## 它能做什么

- **自动读题**：截图 → OCR 识别行/列提示数字 → 交叉校验（行和=列和、可解、三帧一致）才开打
- **自动解题**：完整的数织求解器（含唯一解判定的多种尺寸 5×5 ~ 25×25）
- **自动填格**：分批填色 + 用「游戏自己的提示变灰」当判官，每批自查；空格按需打叉
- **自动连打**：过关后自动回列表、点下一关；读不通的关卡记进跳过表，先去打别的
- **两种关卡列表都会认**：普通谜题（浅黄卡片）与**特别谜题**（5×5 灰「?」方块阵）自动识别切换
- **紧急停止**：F8 / Esc / 把鼠标甩到屏幕左上角

## 下载即用（推荐，不需要装 Python）

1. 下载 **`{name}-{version}-win64.zip`**（上面那个直链，或页面上 Releases 里的 Assets）
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

## 如何使用（一步一步）

### A. 用面板（推荐）

1. 解压 → 双击 **`启动面板.bat`**（面板会自检，必要时自动把源码同步进 exe）
2. 打开游戏，把窗口**切到前台**（别最小化、别被别的窗口盖住）
3. 停在**关卡列表**或**某一关的棋盘**上都行，脚本两种都认
4. 面板上点「**连续闯关**」（只跑一关就点「跑一关」）→ 日志开始刷
5. 想停：点「停止」，或按 **F8 / Esc**，或把鼠标**甩到屏幕左上角**
6. 第一次跑发现定位不准：关掉游戏侧的缩放/多余窗口，或跑一次 `python miku_logic_paint_bot.py --calibrate`

### B. 用命令行

```bat
python miku_logic_paint_bot.py --selftest    :: 先自检（不用开游戏）
python miku_logic_paint_bot.py --loop        :: 连续闯关
python miku_logic_paint_bot.py --dry-run     :: 只识别+求解，不点游戏（想先看它读得准不准）
```

### C. 它什么时候会「不动手」（安全设计）

| 情况 | 它的反应 |
|---|---|
| 认不出画面（既不是棋盘也不是列表） | **一个字都不点**，只等着；不会乱点把画面点乱 |
| 读题对不上（行和≠列和 / 无解 / 三帧不一致） | 先重置本关再读一次；第二次还读不通 → **跳过这关去打别的**（跳过前会确认这一关盘面是干净的） |
| 填色过程中自查没过 | 立刻停手并报出是哪一行/列对不上，不会硬着头皮涂完 |
| 关卡列表布局不认识 | 当作「不是列表」，不点击（两种布局的坐标完全不同，点错会点到棋盘上） |

## 规则

### 游戏规则（它替你自动做的那些事）

| 操作 | 说明 |
|---|---|
| 左键点格子 | 给**空格**涂色（青色）。对「已经有内容」的格子无效——所以一旦某格被误标就涂不上了 |
| 右键点格子 | 给**空格**打叉（灰紫叉），表示这格该空着 |
| 提示数字 | 每行（左侧）与每列（上方）给一串数字，表示这一行/列里**连续涂色块**的长度与顺序，例如 `3 1 2` |
| 怎么算对 | 某一行/列**所有该涂的格子都涂满**时，游戏会把这行/列的提示数字**变灰**——脚本就拿这个当判官 |
| 过关 | 整幅棋盘涂对 → 弹出「关卡完成」→ 回到关卡列表 |
| 特别谜题 | 8 个 Lv 页，每页 25 格拼图，每一格是一道小谜题；解开一格就露出那部分画作 |
| 任务 | 每格 3 个任务：解开谜题 / 无提示 / 无失误 |

### 使用规则（请照着来，否则会失败或伤到你自己的存档）

1. **游戏窗口保持在前台**，别最小化、别被挡住——它是「看屏幕 + 点鼠标」，看不见就读错。
2. **跑的时候别抢鼠标键盘**（除了 F8/Esc 停止）：它会自己移鼠标点格子，你一动鼠标就会点歪。
3. **同一台机器、同一套显示设置**：换了分辨率 / 屏幕缩放 / DPI 之后，重新校准一次（`--calibrate`）。
4. **别在它跑的时候手动涂格**：它按自己解出来的答案涂，你手改一格它下一批自查就会失败停下（不会涂花，但这一关得重来）。
5. **它只做三件事**：截屏识别、移鼠标点击、写本机日志。**不读游戏内存、不改游戏文件、不联网上传**。
6. **自动化可能违反游戏的服务条款**：这是给单人离线小游戏写的自用工具，用不用、风险多大请自己判断（见下面免责声明）。
7. **它会往 `debug/` 存现场截图**（读不通时的画面，只在本机），里面可能有你的桌面内容——**分享这些截图前先看一眼**。

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

## 遇到问题先看《声明》

下载包里有个 **`声明.txt`**，就三件事，跑不顺的时候先看它：

1. **极个别关卡识别不出来** —— 只有 2、3 关有问题（例如 **Lv3-016**、**Lv3-064**，提示数字字太小），
   作者也没解决；建议自己动手填一下，问题关卡不多。
2. **遇到「涂了一半 → 一直重置 → 退出」的循环** —— 直接停掉脚本再启动一次（面板「停止」→「连续闯关」）；
   它跳过一关之前会先确认那一关盘面干净，不会留下没打完的棋。
3. **游戏窗口与分辨率要求** —— 窗口要在前台；推荐 1920×1080 / 1280×720，其它分辨率先 `--calibrate`；
   屏幕缩放保持 100%；别用独占全屏；副屏/投屏/远程桌面会改变截图尺寸。

## 卸载（想删干净的时候）

双击 **`卸载.bat`**，按提示输 `Y` 确认即可。它会：

- 先做两道安全检查：这个目录里必须确实有 `miku_logic_paint_bot.py`，且不能是桌面 / 下载 /
  文档 / 用户目录 / `%TEMP%` / 盘符根目录 / `Windows` / `Program Files` 这类位置 ——
  不通过就直接拒绝执行并说明原因，**绝不误删**；
- 动手前把**将要删除的绝对路径**逐条列出来（没有的项写「（无）」），你确认了才删；
- 关掉正在跑的 `MikuPanel.exe` / 本项目 Python 进程；
- 删掉程序文件夹（含 `debug/` 截图、校准配置、`*.new` 临时备份）；
- 扫 `%TEMP%` 里本程序遗留的 `_MEI*` 临时目录并清掉（只删带本项目标记的那些）。

**不会动** Tesseract-OCR 和 WebView2 —— 那两个是系统级组件，别的软件也可能在用，卸载程序只删自己。
删完程序文件夹会自己消失（窗口提示「已发起删除」后你可以直接关掉它）。

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
卸载.bat                      卸载（删文件夹 + 清运行数据 + 清 %TEMP% 残留）
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

## v1.0.13（《声明》改成作者原话 + 去掉示意图）

**只改说明文字与包里内容，功能零改动。**

- 那份说明改名为 **`声明.txt`**（标题《宇宙声明》→《声明》），第一条按作者原话重写：不铺垫、
  不罗列「已经修好什么」，直说只有 2、3 个关卡有问题（例如 Lv3-016、Lv3-064），建议自己动手填一下。
  README 与发布说明里的同名摘要同步成一样的措辞。
- **去掉三张示意图**：发布包里不再带 `docs/images/`，README 里也不再嵌图。
- 脚本、面板、`启动*.bat`、`卸载.bat` 一个字没动。

## v1.0.12（卸载更安全 + 启动少踩坑）

**卸载不会被「放错位置」坑到。** 上一版的 `卸载.bat` 是按「脚本所在目录」来删的：万一有人把它
单独复制到桌面或下载目录再双击，就会把那个目录整个删掉。这一版加了两道闸门，不通过就拒绝执行：

- **身份校验**：目录里必须确实有 `miku_logic_paint_bot.py`，且 `MikuPanel.exe` / `启动面板.bat`
  至少有一个 —— 认不出来就不删，并提示把它放回程序文件夹。
- **位置黑名单**：桌面、下载、文档、用户目录、`AppData`、`%TEMP%`、任意盘符根目录、`Windows`、
  `Program Files`（含 x86）一律拒绝，防止把整个桌面或整块盘删掉。
- **先列清单**：确认前把要删的绝对路径逐条打印（程序目录、`debug/`、配置、`*.new`、匹配到的
  每个 `_MEI*`），没有的项直接写「（无）」。
- **纵深防御**：真正执行删除的 `/purge` 副本会**再校验一次**目标目录，参数被改坏也删不了。

**启动脚本的防呆**：

- `启动面板.bat`：找不到 `MikuPanel.exe` 时直接明说（多半被杀毒软件删了，重新解压即可），
  而不是双击后「什么都没发生」。
- `启动.bat`：找不到 `miku_logic_paint_bot.py` 时给中文提示，而不是甩一个 Python 报错。

**实测**：把 `卸载.bat` 放到 `%TEMP%` 或空目录 → 拒绝执行、目录完好；真程序目录输 `N` → 什么都不删；
输 `Y` → 程序目录删净、本项目 `_MEI*` 已清、他人 `_MEI*` 保留、临时副本自删。

## v1.0.11（用起来不卡住 + 新增一键卸载）

**症状一：点了没反应，一直读不出来。** 机器上没装 Tesseract-OCR（读提示数字要用的组件）时，
脚本以前只打一行警告就照旧往下跑，用户看到的就是「点了没反应 / 一直读不出来」。

**修法**：改成**启动前硬校验**。缺组件就立刻停下，不再假装在跑，并把「怎么装」写在屏幕上：

- 主脚本 `miku_logic_paint_bot.py`：`init_ocr()` 的返回值以前被丢掉，现在用 `require_ocr()`
  卡住主流程，缺 Tesseract 直接退出（退出码 2）并打印安装指引（含下载页与默认安装路径）。
- `启动面板.bat`：开面板前先预检 **WebView2** 与 **Tesseract**，缺哪个就明确告诉你缺哪个、
  去哪下（WebView2 用微软直链，Tesseract 用 UB-Mannheim 下载页），不再让你对着空窗口猜。

**症状二：想删干净却不知道从哪下手。** 新增 `卸载.bat`（双击即可）：

- 结束正在运行的 `MikuPanel.exe` 与本项目 Python 进程；
- 删除程序文件夹（含 `debug/` 截图、校准配置、`*.new` 临时备份）；
- 扫 `%TEMP%` 里本程序遗留的 `_MEI*` 解包目录，**只删带本项目标记的**（别人的 `_MEI*` 不碰）；
- 父窗口删不掉自己（文件被占用），所以自动把删除动作交给 `%TEMP%` 里的副本，删完自删；
- **不会动** Tesseract-OCR 与 WebView2 —— 系统级组件，别的软件也可能在用，卸载只删自己。

**实测**：输入 `N` 什么都不删；输入 `Y` ——程序文件夹已删、`%TEMP%` 里本项目的 `_MEI*` 已清、
他人 `_MEI*` 保留、临时副本自删；`--selftest` 全过。

## v1.0.10（修复：反复「N×N 校验不通过」——改成逐条提示跨帧投票）

**症状**：同一块棋盘，日志反复刷「5×5（格宽 132.00px）校验不通过」，但用同一帧离线读却能读通。

**根因**：游戏画面一直在轻微动画（计时器、角色、光标所在行列的粉色高亮脉动），
提示格的阈值判断**逐帧翻转** —— 实测同一块棋盘、同一几何：这一帧读得出（行和=列和），
下一帧就丢几行（例如行变成 `[[1,1],[],[],[3],[1,3]]`）。而原来的判定是
「**整帧读数必须两帧完全一致**才算过」，于是永远凑不齐一致 ⇒ 反复校验不通过、整关判死。

**修法**：改成**逐条提示跨帧投票**。每帧把每一条提示线（整行/整列的读数元组）投进票箱，
多帧后按多数票拼回整题，再走原有的校验 + 求解器验证。某一帧某一行抖坏，不再拖垮整关。

**实测**：
- 逐帧像素抖动仿真（同一块棋盘、每帧换一种微扰）：**连续 3 次全部读出正确题面**
  （行 `[[2,2],[5],[4],[1,1],[1,2]]` 和 18 = 列和 18）；
- 离线存档帧（5×5 / 10×10）：读题全通；存档对照 6 帧（含 15×15/20×20）逐行一致、0 差异；
- `--selftest` 全过。
- 兜底仍然严格：投票结果必须同时过「无越界 / 行和=列和 / 可解」，否则一律判失败，
  不会因为投票就放松标准。

## v1.0.9（修复：两位数提示丢掉一个数字 → 整关读不出）

- **提示条带靠棋盘那一端会带上棋盘边框线**，它在条带里几乎满高（实测 8~13px 宽 × 123px 高），
  而同一带的数字只有 67px 高（条带高的 54%）。这条边框被当成第三个「数字块」后，两位数提示
  被拆成（数字1 / 数字2 / 边框）三块、OCR 拼出越界值被否 ⇒ 回退成「整块读」⇒ 只读出一个数字。
  真机症状：Lv1-006 第 0 行真值「2 2」读成 `[2]` ⇒ 行和 16 ≠ 列和 18 ⇒ 整关判死
  （而那一行正是被高亮的行，之前几轮修的高亮/灰字反而把注意力引偏了）。
- 判据用**跨度**（占满整条带）而不是以前试过的**宽度**：宽度过滤在列方向会把正常结构一起干掉
  （见源码里 2026-09-30 的记录），而「占满整条带」只有边框/分隔线才有；且只按垂直于数字排列
  方向那一轴判，避免把裁得很紧的单数字格子误杀。
- 实测（同一台机器、同一帧）：修复前 行和 16 ≠ 列和 18 判死；修复后
  **行 `[[2,2],[5],[4],[1,1],[1,2]]` 和 18 = 列 `[[2],[5],[2],[5],[3,1]]` 和 18**，读题通过 ✓；
  另一关（Lv1-001）行和 11 = 列和 11 一并通过 ✓（无回归）。

## v1.0.8（根因修复：高 DPI 显示器上抓图整体偏移）

- **进程现在按「物理像素」工作**（启动时设 DPI 感知）。在此之前：`GetWindowRect` /
  `ClientToScreen` 返回**逻辑**坐标、`ImageGrab` 却是**物理**像素 —— 显示器缩放不是 100%
  时（实测 150%），「拿逻辑坐标裁物理截图」会整体偏移，抓到的「客户区」偏到窗口左上方、
  棋盘被推出画面 → 定位一律失败 → 报「界面认不出来 / 自动定位失败」，而你屏幕上棋盘清清楚楚。
  修复后同一台机器抓到的客户区从 1280×720（错）变成 **1920×1080（游戏真实渲染分辨率）**，
  棋盘与提示数字完整落在画面内。
  这也解释了「本机能用、换个分辨率就不能用」这一类现象。
  注意：设成物理像素后，旧配置里那份按逻辑坐标存下来的几何会被自动重新定位并覆盖。

## v1.0.7（定位器两处通用修复：小棋盘不再「整屏定位失败」）

1. **横向网格不再被背景稀释**：定位是先纵向定格距、再限定在棋盘高度里找横向网格。
   棋盘只占画面一小块时（背景立绘，或有别的窗口压在游戏上），横向是对「整条带宽度」求投影，
   周期被平掉 → 纵向明明扫出 68.3px 周期、横向恒为 None，整屏报「自动定位失败」。
   现在横向扫不到时，会把条带再切几个子段逐段扫。
2. **局部窗口扫描不能再「抹平画面最外圈」**：那个抹平是给整幅扫描用的（防窗口边框/OSD
   长直线被当成棋盘线、把 5×5 认成 6×6）。但窗口扫的是画面内部，本来就不含那些外框——
   抹平只会把窗口自己最外那几行/列的真实内容覆盖掉。实测同一窗口不抹平能扫出
   5×5@54.6px、抹平后返回空（差异 28195 像素）→ 局部兜底整条失效。

实测（用户 1280×720 真机帧）：修复前整幅/局部候选全为 0；修复后局部能给出候选。
**仍未解决**：那一关的行提示是「5 3」（5+3=8 > 5），说明它其实是 **10 宽**棋盘、不是 5×5；
锁到的格距只覆盖 3 格，线数被吸附成 5×5，提示区裁剪整体错位 → 读题不过。

## v1.0.6（修复：不再「拿过期几何硬读」→ 不再误重置本关）

- 定位失败时**不再退回旧几何去读**，而是返回「没定位到」让脚本继续等画面稳定。
  为什么危险：那份几何可能早就过期（换过关、换过尺寸、改过窗口分辨率），拿它读出来的
  题面会「自洽却错」——行和=列和、还能解出唯一解 —— 于是把一关本来好好的棋盘判成失败，
  **重置本关**（用户看到的「涂到一半被清掉」）。
  宁可晚一点开工，也不误伤你已经涂出来的进度。

## v1.0.5（修复：小棋盘（5×5 / 10×10）定位）

- **取棋盘时，把「画面局部窗口」扫出来的候选一并纳入校验**。以前只在整幅**完全扫不到**时
  才用局部窗口；但还有第二种病：整幅**扫出了错尺寸**（小棋盘被背景纹理带偏成 20×20 之类），
  错候选过不了校验 → 照样报「自动定位失败」→ 退回旧几何读成垃圾。现在两条路都走。
  候选仍要过「读提示数字」校验才会被采用（特别谜题列表那种没有提示数字的画面不会被误当棋盘）。
  实测：用户那张 Lv1（5×5）实时画面 → 采纳 5×5 格宽 132.0px ✓；开场动画盖住提示区的那一帧
  → 判「没有候选过校验」（宁可等，也不拿错几何去读）✓。
  另外顺带修好：一张以前读不出的存档帧，现在能定出真实的 20×20 并采纳 ✓。

## v1.0.4（修复：Lv1 的 5×5 关卡找不到棋盘）

- **修掉「停在 Lv1（5×5）关卡时，脚本报『自动定位失败』，然后退回配置里的旧几何去读、
  读成垃圾」**：定位器原来是「在整幅画面上找格线」，而 5×5 棋盘只占画面一小块、
  又被房间/人物立绘的线稿盖着 → 整幅平均把棋盘信号稀释掉 → 扫不到。
  现在整幅扫不到时会换几个**画面局部窗口**再扫一遍（真机实测：整幅=空，
  局部窗口稳定给出 5×5、格距 132px）。候选仍要过「读提示数字」校验才会被采用，
  所以特别谜题列表（本身就是 5×5 方块阵）不会被误当成棋盘。

## v1.0.3（修复：下载包双击打不开面板）

- **修掉「双击 `启动面板.bat` 报『重新打包失败 → 这次先不开面板』」**：下载来的发布包里没有
  PyInstaller，读不了 exe 内部，旧脚本把「没法自检」当成了「内容不一致」，接着去重新打包
  （同样因为没有 PyInstaller 而失败），于是拒绝开面板。现在**检测不到 PyInstaller 就跳过
  自检直接开面板**（包里 exe 与主脚本本来就是一起打出来的，不存在跑错版本）。
  ⚠ v1.0.2 的包有这个毛病，**请用 v1.0.3**（v1.0.2 的 Release 已删除，它的读题修复已含在本版）。

## v1.0.2（读题修复二：灰字）

- **修掉「游戏已满足、变灰的提示数字读不出来」**：游戏把已经满足的行/列提示整块染成灰
  （字 165~215、底色 231），而条带阈值会被混进条带的深色界面拉到 ~142 → 灰字整块消失，
  连「11」这种两位数都会从读数里丢掉（真机 20×20 上表现为行和≠列和、这关反复读不出来）。
  现在切分子块时会用「明显比底色暗」的宽阈值兜一次，让既有的「细窄笔画=1」规则拼回 11。
  实测：4 张以前读失败的真机帧，改后与**游戏当年确认过的读数逐行全等（差异 0 处）**。

## v1.0.1（读题修复）

- **修掉一类「提示数字读错」的缺陷**：形如「13」的两位数会被整块 OCR 读成「15」，
  而"3 / 5 字形定夺"机制的门槛写错（只覆盖 12% 的「3」），导致错读数被采纳 →
  行和≠列和、整帧判死（真机 Lv3-016 就是这么读不出来的）。
  现按 1432 张真机帧、7156 个「3」+ 4444 个「5」的统计重定门槛（≤0.60 判 3 / ≥0.62 判 5）。
  实测：20 张真机失败帧由 1/20 → **19/20** 读出，0 帧变差；**Lv3-016 真机复测通过**（读数自洽、可解）。

## v1.0.0（首次公开发布）

- 自动读题：截图 + OCR + 行/列交叉校验（行和=列和、可解、三帧一致），支持 5×5 ~ 25×25
- 自动解题与填格：分批填色，每批用「游戏自己把提示变灰」当判官自查；空隙可自动打叉
- 自动连打：过关自动回列表并继续；读不通的关卡记入跳过表、先打别的（跳过前先确认盘面干净）
- **两种关卡列表自动识别**：普通谜题（浅黄卡片）与特别谜题（5×5 灰「?」方块阵），
  识别判据全部来自画面本身（格线、灰挡板、黄底占比、有没有棋盘），认不出就一个字都不点
- 面板 GUI：本地点按钮启动/停止、实时日志、模式与参数勾选
- 紧急停止：F8 / Esc / 鼠标甩到屏幕左上角
- 安全闸门：点击前确认「眼前是棋盘」、盲点不落在棋盘上、过关后走菜单退出而不是乱点
"""

UNIVERSE_TXT = """《声明》

一、极个别关卡识别不出来

我研究半天也没解决就2/3个关卡有点问题，例如lv3-16,lv3-64
这个时候建议自己动手填一下，问题关卡不多


二、遇到「涂了一半 → 一直重置 → 退出」的循环，直接重启脚本

如果某一关反复出现：重置本关 → 重新识别 → 又失败 → 退出回列表 → 又点回同一关，
说明这一关的读数一直不可信。

处理办法：把脚本停掉再启动一次（面板上点「停止」，再点「连续闯关」）。
重启后它会重新扫描列表，通常就绕过去了。

放心的一点：脚本在跳过一关之前会确认那一关盘面是干净的，
不会给你留下一盘涂了一半的棋（所以最多是多花点时间，不会弄坏存档）。


三、游戏窗口与分辨率要求

· 游戏窗口必须在前台：不要最小化、不要被别的窗口挡住。它是「截屏 + 点鼠标」，
  看不见画面就会读错。
· 推荐客户区 1920×1080 或 1280×720。其它分辨率也能用，但第一次先校准一次：
      python miku_logic_paint_bot.py --calibrate
· 屏幕缩放（DPI）保持 100% 最稳；改过缩放或换过分辨率之后，要重新校准一次。
· 游戏里建议用「窗口化」或「无边框窗口」，别用独占全屏。
· 副屏、投屏、远程桌面都可能让截图尺寸变化，跑之前把游戏放回主屏。


就这三条。跑得顺就忘了它；跑不顺，回来看一眼。
"""

RELEASE_NOTES = """## 下载

| 文件 | 直链 |
|---|---|
| 运行包（含面板 exe，免安装） | <https://github.com/{owner}/{name}/releases/download/v{version}/{name}-{version}-win64.zip> |
| 源码包 | <https://github.com/{owner}/{name}/releases/download/v{version}/{name}-{version}-src.zip> |

| 文件 | 给谁 |
|---|---|
| `{name}-{version}-win64.zip` | **想直接用**：解压 → 双击 `启动面板.bat`。自带面板 exe，不用装 Python |
| `{name}-{version}-src.zip` | **想改代码**：源码树，`pip install -r requirements.txt` 后用 Python 跑 |

两者都还需要本机装 **Tesseract-OCR**（读提示数字用）与 **WebView2**（只有面板 GUI 需要，Win11 自带）。

## 本版（v{version}）改了什么

- **说明改名 + 更新**：那份说明由《宇宙声明》改名为 **《声明》**（文件 `声明.txt`），
  第一条按作者原话改写 —— 只有 2、3 关有问题（例如 Lv3-016、Lv3-064），建议自己动手填一下，
  去掉多余的铺垫。README 与发布说明里的同名摘要同步成一样的措辞。
- **去掉三张示意图**：发布包里不再带 `docs/images/`，README 里也不再嵌图。
- 除说明文字外，**功能零改动**（脚本、面板、启动/卸载脚本都没动）。

## 上一版（v1.0.12）改了什么

- **卸载不会被「放错位置」坑到**：`卸载.bat` 现在先做**身份校验**（目录里得有
  `miku_logic_paint_bot.py`，且 `MikuPanel.exe` / `启动面板.bat` 至少有一个）+ **位置黑名单**
  （桌面 / 下载 / 文档 / 用户目录 / `%TEMP%` / 盘符根目录 / `Windows` / `Program Files` 一律拒绝），
  不通过就拒绝执行并说明原因；确认前还会把**将要删除的绝对路径**逐条列出来；
  真正动手的 `/purge` 副本会**再校验一次**目标目录。
- **启动防呆**：`启动面板.bat` 找不到 `MikuPanel.exe`、`启动.bat` 找不到主脚本时，都会给中文说明，
  而不是双击后「没反应」或甩一个 Python 报错。
- 上一版的「缺组件不再静默卡住」保持不变。

## v1.0.11 改了什么

- **不再「点了没反应」**：没装 Tesseract-OCR 时，以前只打一行警告就继续跑，看起来像卡住。
  现在启动前就硬校验，缺组件立刻停下，并把「去哪下、装到哪」直接打印出来；
  `启动面板.bat` 也会先预检 WebView2 与 Tesseract，缺哪个告诉你哪个。
- **新增 `卸载.bat`（一键卸载）**：双击 → 输 `Y`，自动结束进程、删程序文件夹（含 `debug/` 截图、
  校准配置、临时备份）、清 `%TEMP%` 里本程序遗留的 `_MEI*` 目录。**不动** Tesseract-OCR / WebView2。
  删完文件夹自己消失，窗口提示「已发起删除」后可直接关掉。

- **读题（两处）**：
  1. 「两位数被整块读错」（「13」读成「15」）——按 1432 张真机帧重定「3 / 5」字形定夺门槛；
  2. 「游戏已满足、变灰的提示整块读不出」——切分子块时用宽阈值兜一次。
  实测：20 张真机失败帧由 1/20 → **19/20** 读出；4 张以前失败的存档帧改后与
  **游戏确认过的读数逐行全等（差异 0 处）**；Lv3-016 真机复测通过。
- **启动**：修掉下载包双击 `启动面板.bat` 打不开面板的问题（没装 PyInstaller 的机器上
  旧脚本会「自检失败 → 重打包失败 → 拒绝开面板」，现在直接跳过自检开面板）。
- **小棋盘（5×5 / 10×10）定位**：取棋盘时把「局部窗口」扫描的候选一并纳入校验，
  整幅扫不到、或者整幅给出错尺寸，两种病都能兜住。

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

## 先看这一份：《声明》.txt

包里有一份 `声明.txt`，就三件事：

1. **极个别关卡识别不出来** —— 只有 2、3 关有问题（例如 **Lv3-016**、**Lv3-064**，提示数字字太小），
   作者也没解决；建议自己动手填一下，问题关卡不多。
2. **遇到「涂了一半 → 一直重置 → 退出」的循环** —— 把脚本停掉再启动一次（面板「停止」→「连续闯关」）；
   它跳过一关之前会先确认那一关盘面是干净的，不会给你留下没打完的棋。
3. **窗口与分辨率要求** —— 游戏窗口要在前台；推荐客户区 1920×1080 或 1280×720，其它分辨率先 `--calibrate`；
   屏幕缩放保持 100%；别用独占全屏；副屏/投屏/远程桌面会改变截图尺寸。
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
    (dst / "README.md").write_text(
        README.format(title=TITLE, name=NAME, owner=OWNER, version=version), encoding="utf-8")
    (dst / "LICENSE").write_text(LICENSE.replace("{year}", "2026").replace("{name}", NAME),
                                 encoding="utf-8")
    (dst / "CHANGELOG.md").write_text(CHANGELOG.format(version=version), encoding="utf-8")
    # 声明：带 BOM，Windows 记事本双击也不乱码
    (dst / "声明.txt").write_text(UNIVERSE_TXT, encoding="utf-8-sig")
    docs = dst / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "发布说明.md").write_text(
        RELEASE_NOTES.format(name=NAME, owner=OWNER, version=version),
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
- [ ] 包里三份说明都在：`README.md`、`声明.txt`、`CHANGELOG.md`
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
