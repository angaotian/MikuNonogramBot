# Miku 数织自动闯关（Hatsune Miku Logic Paint S 辅助工具）

Windows 上给《Hatsune Miku Logic Paint S》里那套数织（nonogram / 数方）小游戏做的**自动闯关工具**。
它看着游戏窗口截图，自己认提示数字、求解、点格子、过关、再点下一关；带一个本地面板可以点按钮，
也可以纯命令行跑。

> 非官方项目，与游戏发行商/开发商无关。仅供个人学习与自用，请自行遵守游戏的服务条款。

## ⬇️ 下载（Windows，免安装）

**[点这里直接下载 MikuNonogramBot-1.0.0-win64.zip](https://github.com/angaotian/MikuNonogramBot/releases/latest/download/MikuNonogramBot-1.0.0-win64.zip)**（约 32 MB，自带面板，不用装 Python）

- 想下最新版永远用这个地址：<https://github.com/angaotian/MikuNonogramBot/releases/latest>
- 只想看代码：[MikuNonogramBot-1.0.0-src.zip](https://github.com/angaotian/MikuNonogramBot/releases/latest/download/MikuNonogramBot-1.0.0-src.zip)
- 下载后：**解压 → 双击 `启动面板.bat` → 把游戏停在关卡列表或棋盘上 → 点「连续闯关」**

> 在 GitHub 网页上找不到下载位置的话：页面**右侧边栏**的 **Releases → v1.0.0**，或页面底部 **Releases** 区域，点进去最下面就是 **Assets**（两个 zip）。

## 它能做什么

- **自动读题**：截图 → OCR 识别行/列提示数字 → 交叉校验（行和=列和、可解、三帧一致）才开打
- **自动解题**：完整的数织求解器（含唯一解判定的多种尺寸 5×5 ~ 25×25）
- **自动填格**：分批填色 + 用「游戏自己的提示变灰」当判官，每批自查；空格按需打叉
- **自动连打**：过关后自动回列表、点下一关；读不通的关卡记进跳过表，先去打别的
- **两种关卡列表都会认**：普通谜题（浅黄卡片）与**特别谜题**（5×5 灰「?」方块阵）自动识别切换
- **紧急停止**：F8 / Esc / 把鼠标甩到屏幕左上角

## 下载即用（推荐，不需要装 Python）

1. 下载 **`MikuNonogramBot-1.0.0-win64.zip`**（上面那个直链，或页面上 Releases 里的 Assets）
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
| **Tesseract-OCR** | 装在 `C:\\Program Files\\Tesseract-OCR\\` 或加入 PATH（脚本会自动找） |
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
--list-mode {auto,normal,special}   关卡列表布局：默认 auto 自动识别
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
- 命令行 `python debug\probe_special_list.py --live`（那是开发用的探针，**不随发布包提供**，只在本项目的开发目录里）
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
python tools\\panel_verify.py     :: 先看 exe 与真源是否一致（内容物 / 副本 / 运行环境）
python tools\\panel_build.py      :: 打包（写 .new → 全量体检 → 备份旧版 → 原子替换）
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
