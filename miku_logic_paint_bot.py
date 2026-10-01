#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hatsune Miku Logic Paint S 自动闯关脚本（Windows / Steam 版）

工作原理：
  1. 用 Win32 API 找到游戏窗口；棋盘靠画面里的网格线自动定位与识别尺寸
     （支持 5×5 ~ 20×20 等方形尺寸，全部是正方形；换关会自动重新定位）
  2. 截取窗口客户区画面 -> 按格子切开提示数字区域 -> Tesseract OCR 识别
     （多线程 + 两帧共识；差一两个数字时用「行列总和相等」约束 + 求解器自动纠正）
  3. 内置数织求解器（约束传播 + 分支搜索）求出完整答案
  4. 用 Win32 原生鼠标事件逐格点击：左键填色、右键打叉
     （填色前先把该空着的格子右键打叉，因为填完最后一格会立刻过关结算）
  5. 懒人循环：自动等新棋盘 -> 识别 -> 求解 -> 填格 -> 过关 -> 继续；
     涂过一半（提示数字变灰）的关卡会自动「菜单 → 重置」重开，直到按停止键

常用命令：
  python miku_logic_paint_bot.py --calibrate    首次校准（必须先让游戏停在某一关棋盘界面）
  python miku_logic_paint_bot.py --loop         连续自动闯关
  python miku_logic_paint_bot.py                只打当前这一关
  python miku_logic_paint_bot.py --dry-run      只识别 + 求解，不点击（强烈建议先跑一次）
  python miku_logic_paint_bot.py --goto         把鼠标移到左上角第一格中心（可 --goto 2,3 指定格子）
  python miku_logic_paint_bot.py --probe        诊断当前画面（保存标注截图到 debug/）
  python miku_logic_paint_bot.py --selftest     不需要游戏的自检

紧急停止：F8 / Esc / 把鼠标甩到屏幕左上角
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import itertools
import json
import math
import os
import random
import re
import shutil
import sys
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageOps

try:
    import pytesseract
except Exception:  # pragma: no cover
    pytesseract = None

try:  # 避免 GBK 控制台里个别字符直接抛异常
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "miku_bot_config.json"
DEBUG_DIR = ROOT / "debug"
# 本次运行实际使用的配置文件。main() 会按 --config 覆盖它；凡是「顺手把新定位到的
# 棋盘几何 / 星数记录写回去」的地方都写这个路径，免得传了 --config 却写进默认那份。
CONFIG_PATH = DEFAULT_CONFIG

# 游戏支持的方形尺寸（吸附/校验用），含 15×15、25×25
STANDARD_SIZES = (5, 10, 15, 20, 25)
# 游戏里实际会出现的方形尺寸（5×5 ~ 20×20，另留 25 以防更高难度）
SQUARE_SIZES = tuple(range(5, 21)) + (25,)
DEFAULT_TITLE_KEYWORDS = ("Logic Paint", "Hatsune", "Miku")

VK_F7, VK_F8, VK_ESCAPE = 0x76, 0x77, 0x1B
SW_MINIMIZE, SW_RESTORE = 6, 9
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON = 0x0201, 0x0202, 0x0001
WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON = 0x0204, 0x0205, 0x0002

# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class BotAbort(Exception):
    """需要中止当前流程（用户按下停止键、窗口丢失、识别失败等）。"""


class SolverTimeout(Exception):
    pass


# --------------------------------------------------------------------------
# Win32 基础封装
# --------------------------------------------------------------------------


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.IsIconic.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.BringWindowToTop.restype = wt.BOOL
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.mouse_event.argtypes = [wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
kernel32.GetConsoleWindow.restype = wt.HWND


def set_dpi_awareness() -> None:
    """让截图 / 鼠标坐标都使用真实物理像素，避免缩放导致的偏移。"""
    try:
        # PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_pid(hwnd: int) -> int:
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_name(pid: int) -> str:
    """取进程可执行文件名（小写）。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def our_panel_process(pname: str, title: str = "") -> bool:
    """这个窗口是不是**我们自己的控制面板**？（MikuPanel.exe）

    面板窗口标题里带「Miku」「数织」，跟游戏的标题关键字一模一样；它的进程名
    `MikuPanel.exe` 又跟游戏（`HatsuneMikuLogicPaintS.exe`）不像。所以只按标题兜底匹配时
    它会被当成游戏 —— 真机 2026-10-01 踩过一次：脚本对着面板窗口连点「继续」。
    面板是我们自己的界面，任何情况下都不该被当成游戏。
    """
    p = (pname or "").lower()
    if "mikupanel" in p:
        return True
    t = title or ""
    return "自动闯关" in t and "控制面板" in t


user32.GetClassNameW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wt.DWORD,
                                                ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.GetCurrentProcessId.restype = wt.DWORD


def find_game_window(keywords: Sequence[str] = DEFAULT_TITLE_KEYWORDS) -> Optional[int]:
    """
    找游戏窗口。优先按「进程名」匹配（HatsuneMikuLogicPaintS.exe），
    标题匹配只作为兜底，并跳过控制台窗口与本进程的窗口
    （否则 .bat 把控制台标题设成含 Miku 字样时会被误认成游戏）。

    ⚠ 2026-10-01 补：**还要跳开我们自己的控制面板**（MikuPanel.exe / 标题含「数织自动闯关」）。
    面板标题里有「Miku」「数织」，跟标题关键字一模一样，一旦游戏那侧的进程名一时取不到
    （降权成只按标题匹配）它就会赢 —— 真机实测过一次：脚本把面板当游戏，
    对着面板窗口连点「继续」，日志里窗口标题写着「Miku 数织自动闯关 · 控制面板」。
    面板是我们自己的界面，永远不可能是游戏，所以直接排除。
    """
    kws = [k.lower() for k in keywords if k]
    me = int(kernel32.GetCurrentProcessId())
    hits: List[Tuple[int, int]] = []

    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        r = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(r))
        if (r.right - r.left) < 200 or (r.bottom - r.top) < 200:
            return True
        if _window_class(hwnd) == "ConsoleWindowClass":
            return True
        pid = _window_pid(hwnd)
        if pid == me:
            return True
        pname = _process_name(pid)
        title = _window_title(hwnd)
        if our_panel_process(pname, title):
            return True                     # 我们自己的控制面板，不可能是游戏
        score = 0
        if "hatsunemiku" in pname or "logicpaint" in pname:
            score = 3
        title = title.lower()
        if title and any(k in title for k in kws):
            score = max(score, 2)
        if score:
            hits.append((score, hwnd))
            if score >= 3:
                return False
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    if not hits:
        return None
    hits.sort(key=lambda it: it[0], reverse=True)
    return hits[0][1]


def client_origin(hwnd: int) -> Tuple[int, int]:
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def client_size(hwnd: int) -> Tuple[int, int]:
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


def ensure_foreground(hwnd: int, timeout: float = 3.0) -> bool:
    """截图前必须让游戏在最前面，否则会截到别的窗口。

    SetForegroundWindow 有时会被系统的「前台锁定」拦下（比如刚点过控制台窗口），
    这时改成 AttachThreadInput 先挂到当前前台线程上再抢，成功率明显更高；
    不然脚本会在启动时直接报「无法把游戏窗口切到前台」中止。
    """
    end = time.time() + timeout
    while time.time() < end:
        if user32.GetForegroundWindow() == hwnd:
            return True
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        tid_self = kernel32.GetCurrentThreadId()
        attached = False
        try:
            if tid_fg and tid_fg != tid_self:
                attached = bool(user32.AttachThreadInput(tid_fg, tid_self, True))
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        finally:
            if attached:
                try:
                    user32.AttachThreadInput(tid_fg, tid_self, False)
                except Exception:
                    pass
        time.sleep(0.12)
    return user32.GetForegroundWindow() == hwnd


_UI_POINTS: List[Tuple[float, float]] = []      # 最近算出来的 UI 点（重置/继续等按钮）
_GUARD_GEOM = None                              # 非 None 时开启「棋盘点击审计」
_PLANNED_CLICKS = False                         # 只有 execute_fill/execute_marks 期间为 True
_PLANNED_SET: set = set()                       # 当前这批「该点的格子」；填色期间落点必须在这个集合里
_STRAY_CLICKS: List[Tuple[float, float, Tuple[float, float]]] = []


CLICK_SETTLE_S = 0.045      # SetCursorPos 与 mouse_event 之间的等待（见 click_screen 说明）


def click_screen(x: float, y: float, hold: float = 0.016, right: bool = False,
                 settle: float = CLICK_SETTLE_S) -> None:
    """原生鼠标点击：按下与抬起之间留一点时间，避免 Unity 同一帧内漏掉点击。

    right=False 左键（填色），right=True 右键（打叉）。

    ★ 2026-09-30 凌晨：**移动光标和注入点击之间必须留一点时间**。
    原来的写法是「SetCursorPos(目标) → 立刻 mouse_event(按下)」，但
    SetCursorPos 对 Windows 输入队列是**异步**的：注入的按下可能带着**旧的光标
    位置**被游戏收到，于是这一下落到**上一格**上——目标格空着、邻格被涂。
    症状（Lv3-028 20×20 现场存档实测）：
      · 列14：脚本账本说"该列涂完了"，游戏却一直不变灰 → 收尾自查判"读题错"→ 重置；
      · 列20：脚本账本说"还没涂完"，游戏却已经把它变灰 → 说明有格子被涂了却没记账。
    两者都只有"点击落点与脚本以为的不一致"能解释，而回读校验用同一套坐标，
    永远看不见（它读到的就是脚本自己以为的位置）。
    25ms 的等待对总时长影响可忽略（一格一下，一整关 193 格 ≈ +5 秒），
    但对 20×20（格宽 34px）这种小格子是生与死的差别。
    2026-09-30 17:41 的现场（列10 失败，账本说涂完游戏没变灰 + 行20/列20 反向矛盾）
    说明**25ms 仍会偶发**，已提到 45ms；同时建议面板里的「点击间隔」也调大到 80~120ms。
    """
    user32.SetCursorPos(int(round(x)), int(round(y)))
    if settle > 0:
        time.sleep(settle)
    down = MOUSEEVENTF_RIGHTDOWN if right else MOUSEEVENTF_LEFTDOWN
    up = MOUSEEVENTF_RIGHTUP if right else MOUSEEVENTF_LEFTUP
    user32.mouse_event(down, 0, 0, 0, None)
    if hold > 0:
        time.sleep(hold)
    user32.mouse_event(up, 0, 0, 0, None)


def key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def cursor_pos() -> Tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def park_cursor(screen: "Screen") -> None:
    """把鼠标挪到窗口角落：游戏会把光标所在的行/列提示高亮成粉色，挪开可避免干扰识别。"""
    hwnd = getattr(screen, "hwnd", None)
    if not hwnd:
        return
    try:
        ox, oy = client_origin(hwnd)
        user32.SetCursorPos(int(ox + 8), int(oy + 8))
    except Exception:
        pass


def _board_indices(geom: "Geometry", x: float, y: float) -> Tuple[int, int]:
    """客户区坐标 (x, y) 落在棋盘的哪一格？返回 **(0 基)** 的 (行, 列)。

    ⚠ 必须和 `Geometry.cell_center(r, c) = x1 + c*cell_w` 同一套口径（0 基）——
    2026-10-01 真机跑特别谜题时发现：这里原来算的是 1 基（`round(...)+1`），
    跟 `_PLANNED_SET`（0 基）一比就永远差一格，于是 58 次计划内的点击里有 39 次被
    误报成「点歪了」。判据是那条关卡最后照样过关、而且逐帧比对显示变化的正好是
    计划内那 24 格——所以是审计自己算错了下标，不是点击歪了。
    """
    return (int(round((y - geom.y1) / geom.cell_h)),
            int(round((x - geom.x1) / geom.cell_w)))


def minimize_console() -> None:
    hwnd = kernel32.GetConsoleWindow()
    if hwnd:
        user32.ShowWindow(hwnd, SW_MINIMIZE)


# --------------------------------------------------------------------------
# 屏幕对象（真实 / 自检用假对象共用同一套接口）
# --------------------------------------------------------------------------


class Screen:
    def __init__(self, hwnd: int):
        self.hwnd = hwnd

    def alive(self) -> bool:
        return bool(self.hwnd) and bool(user32.IsWindow(self.hwnd))

    def ensure_foreground(self) -> None:
        if not ensure_foreground(self.hwnd):
            raise BotAbort("无法把游戏窗口切到前台（是不是被最小化了？请先恢复窗口再运行）")

    def grab_client(self) -> Image.Image:
        """截取客户区：只能前台抓（游戏窗口必须可见、没被最小化）。"""
        if not self.alive():
            raise BotAbort("游戏窗口已关闭")
        self.ensure_foreground()
        ox, oy = client_origin(self.hwnd)
        cw, ch = client_size(self.hwnd)
        img = ImageGrab.grab(bbox=(ox, oy, ox + cw, oy + ch), all_screens=True)
        return img.convert("RGB")

    def click_client(self, x: float, y: float, hold: float = 0.016,
                     right: bool = False) -> None:
        # 该游戏（Unity）不响应 PostMessage 合成的鼠标消息（实测点击完全无效），
        # 所以只能用真实鼠标输入。真实点击只会落在「鼠标位置最上层的窗口」上，
        # 因此游戏窗口必须保持可见、不能被别的窗口盖住。
        #
        # **棋盘点击审计**：这一下如果点在棋盘格子里、却既不是计划内的填色/打叉，
        # 也不在已知 UI 按钮附近，就记一笔并打警告——这类「不知从哪来的一下」正是
        # 人眼看到的"误点"。只记录不拦截（重置对话框的按钮可能正好压在棋盘上方）。
        if _GUARD_GEOM is not None:
            g = _GUARD_GEOM
            if (g.x1 - g.cell_w / 2 <= x <= g.x2 + g.cell_w / 2
                    and g.y1 - g.cell_h / 2 <= y <= g.y2 + g.cell_h / 2
                    and not any(abs(x - ux) <= 12 and abs(y - uy) <= 12
                                for ux, uy in _UI_POINTS)):
                row, col = _board_indices(g, x, y)
                if _PLANNED_CLICKS:
                    # 计划内的填色/打叉期间：落点**必须**是本批清单里的那一格。
                    # 落在别的棋盘格上（哪怕那格也是计划内的、只是属于别的批次）就是点歪了——
                    # 它会顶掉本批那一格的点击、让目标格一直空着，直到收尾自查才报"没变灰"。
                    # 真机 Lv3-028 就是这么"开头看着误点、快做完了才报警"的（2026-09-30 凌晨补）。
                    if _PLANNED_SET and (row, col) not in _PLANNED_SET:
                        _STRAY_CLICKS.append((x, y, (row, col)))
                        print(f"  [审计] 填色/打叉期间这一点落在第 {row + 1} 行第 {col + 1} 列，"
                              f"不在本批 {len(_PLANNED_SET)} 格清单里——点歪了"
                              f"（客户区 ({x:.0f},{y:.0f})）")
                else:
                    _STRAY_CLICKS.append((x, y, (row, col)))
                    print(f"  [审计] 这一下点击落在棋盘格第 {row + 1} 行第 {col + 1} 列，"
                          f"但它不是计划内的填色/打叉：客户区 ({x:.0f},{y:.0f})")
        ox, oy = client_origin(self.hwnd)
        click_screen(ox + x, oy + y, hold, right)

    def move_client(self, x: float, y: float) -> None:
        """只移动鼠标到客户区坐标 (x, y)，不点击。"""
        ox, oy = client_origin(self.hwnd)
        user32.SetCursorPos(int(round(ox + x)), int(round(oy + y)))

    def move_to_cell(self, geom: "Geometry", r: int = 0, c: int = 0) -> Tuple[float, float]:
        """把鼠标移到第 r 行第 c 列格子的中心，返回该格的客户区中心坐标。"""
        cx, cy = geom.cell_center(r, c)
        self.move_client(cx, cy)
        return cx, cy


# --------------------------------------------------------------------------
# 停止控制
# --------------------------------------------------------------------------


class StopController:
    def __init__(self, cfg: "Config", log=None):
        self.cfg = cfg
        self.log = log or (lambda *_: None)
        self.reason: Optional[str] = None

    def check(self) -> None:
        """在点击/等待循环里频繁调用；命中停止条件就抛 BotAbort。"""
        if self.reason:
            raise BotAbort(f"已停止：{self.reason}")
        if key_down(VK_F8):
            raise BotAbort("手动停止（F8）")
        if key_down(VK_ESCAPE):
            raise BotAbort("手动停止（Esc）")
        if self.cfg.failsafe_corner:
            x, y = cursor_pos()
            if x <= 2 and y <= 2:
                raise BotAbort("触发防呆（鼠标甩到屏幕左上角）")

    def sleep(self, seconds: float, step: float = 0.05) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.check()
            time.sleep(min(step, max(0.0, end - time.time())))

    def wait_key(self, vks: Sequence[int], timeout: Optional[float] = None) -> Optional[int]:
        """等待其中某个键被按下并松开（用于校准）。"""
        end = None if timeout is None else time.time() + timeout
        while True:
            if end is not None and time.time() > end:
                return None
            for vk in vks:
                if key_down(vk):
                    while key_down(vk):
                        time.sleep(0.02)
                    return vk
            time.sleep(0.02)


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


@dataclass
class Geometry:
    x1: float          # 左上格中心（客户区坐标）
    y1: float
    x2: float          # 右下格中心
    y2: float
    cols: int
    rows: int
    row_slots: Optional[int] = None   # 行提示格数（默认 ceil(cols/2)）
    col_slots: Optional[int] = None

    @property
    def cell_w(self) -> float:
        return (self.x2 - self.x1) / max(1, self.cols - 1)

    @property
    def cell_h(self) -> float:
        return (self.y2 - self.y1) / max(1, self.rows - 1)

    @property
    def max_row_slots(self) -> int:
        return int(self.row_slots or (self.cols + 1) // 2)

    @property
    def max_col_slots(self) -> int:
        return int(self.col_slots or (self.rows + 1) // 2)

    def cell_center(self, r: int, c: int) -> Tuple[float, float]:
        return self.x1 + c * self.cell_w, self.y1 + r * self.cell_h

    def to_dict(self) -> dict:
        return {"x1": self.x1, "y1": self.y1, "x2": self.x2, "y2": self.y2,
                "cols": self.cols, "rows": self.rows,
                "row_slots": self.row_slots, "col_slots": self.col_slots}


@dataclass
class Config:
    window_keywords: List[str] = field(default_factory=lambda: list(DEFAULT_TITLE_KEYWORDS))
    geom: Optional[Geometry] = None
    next_button: Optional[List[float]] = None   # 过关后要点的按钮（客户区坐标）
    click_hold_ms: float = 16.0
    click_interval_ms: float = 10.0
    fills_per_pause: int = 24
    pause_ms: float = 40.0
    max_read_attempts: int = 3
    ocr_workers: int = 1        # ⚠ 2026-09-30：默认改 1（单线程）。
    # 原因：多线程时 OCR 的候选集**带随机性**——同一帧两次读出来的结果可能不同
    # （实测 326/324 与 326/304 都出现过），于是**同一关每次跑读数可能都不一样**，
    # 这正是"有时读得出有时读不出"的机制之一。单线程实测单帧约 90 秒，
    # 与 8 线程相当（瓶颈不在并行度），所以确定性的收益远大于那点速度。
    # 想恢复多线程：改这里或设 MIKU_BOT_OCR_WORKERS=<n>。
    solver_time_limit: float = 20.0
    paint_diff_threshold: float = 8.0
    max_reclick_cells: int = 5
    next_button_delay: float = 1.4
    clear_wait_timeout: float = 30.0
    auto_confirm: bool = True
    failsafe_corner: bool = True
    mark_empty: bool = True        # 填完颜色后，用右键把「该留空」的格子打上叉
    # 关卡列表布局：auto=按画面自动认（普通谜题黄卡片 / 特别谜题灰方块阵），
    # normal=只认普通谜题列表，special=只认特别谜题列表。
    # 面板外壳（MikuPanel.exe）只认固定的几个 --xxx 参数，没法从界面传这个开关，
    # 所以自动识别是默认；要强制指定就改这个配置项或命令行加 --list-mode。
    list_mode: str = "auto"

    @staticmethod
    def load(path: Path) -> "Config":
        cfg = Config()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[警告] 配置文件读取失败（{exc}），使用默认配置")
                return cfg
            for key, val in raw.items():
                if key == "geom" and isinstance(val, dict):
                    try:
                        g: Optional[Geometry] = Geometry(**val)
                    except Exception:
                        g = None
                    # 只保留受支持的方形尺寸；非方形/异常尺寸一律作废，强制重新定位
                    if g is not None and (g.rows != g.cols or g.rows not in STANDARD_SIZES):
                        g = None
                    cfg.geom = g
                elif hasattr(cfg, key):
                    setattr(cfg, key, val)
        return cfg

    def save(self, path: Path) -> None:
        data = {k: v for k, v in self.__dict__.items() if k != "geom"}
        data["geom"] = self.geom.to_dict() if self.geom else None
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------

_TESSERACT_CMD: Optional[str] = None
_OCR_CACHE: Dict[str, Optional[int]] = {}
OCR_TIMEOUT = 4.0        # 单次 Tesseract 调用的超时（秒）：正常只要几十毫秒


def find_tesseract() -> Optional[str]:
    cands = [
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        str(Path.home() / "AppData/Local/Programs/Tesseract-OCR/tesseract.exe"),
    ]
    for c in cands:
        if c and Path(c).exists():
            return str(c)
    return None


def init_ocr() -> bool:
    global _TESSERACT_CMD
    if pytesseract is None:
        print("[警告] 未安装 pytesseract，无法识别提示数字（pip install pytesseract）")
        return False
    cmd = find_tesseract()
    if not cmd:
        print("[警告] 未找到 Tesseract-OCR，请先安装：https://github.com/UB-Mannheim/tesseract/wiki")
        return False
    _TESSERACT_CMD = cmd
    pytesseract.pytesseract.tesseract_cmd = cmd
    return True


def _otsu_threshold(gray: np.ndarray) -> int:
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return 127
    sum_all = float(np.dot(np.arange(256), hist))
    sum_b = 0.0
    w_b = 0.0
    best, thr = -1.0, 127
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best:
            best, thr = var, t
    return thr



def ocr_cell(crop: Image.Image, thr: Optional[float] = None,
             mid: Optional[float] = None) -> Optional[int]:
    """识别单个提示数字，返回最可信的一个值（失败返回 None）。"""
    cands = ocr_cell_candidates(crop, thr, mid)
    return cands[0] if cands else None


def ocr_cell_candidates(crop: Image.Image, thr: Optional[float] = None,
                        mid: Optional[float] = None) -> List[int]:
    """
    识别单个提示数字，按可信度返回候选值列表（可能为空）。
    thr/mid 为条带级二值化阈值与底色亮度：用多个阈值分别识别再投票，
    可以缓解笔画略细时「6」被读成「7」这类问题；候选列表用于后续按
    行列总和约束自动纠错。
    """
    if pytesseract is None or _TESSERACT_CMD is None:
        return []
    tkey = "" if thr is None else f"|{thr:.0f}|{'' if mid is None else f'{mid:.0f}'}"
    key = hashlib.blake2b(crop.tobytes(), digest_size=8).hexdigest() + f"|{crop.size}{tkey}"
    if key in _OCR_CACHE:
        return list(_OCR_CACHE[key])
    val = _ocr_cell_uncached(crop, thr, mid)
    if len(_OCR_CACHE) > 6000:
        _OCR_CACHE.clear()
    _OCR_CACHE[key] = tuple(val)
    return list(val)


def _ocr_cell_uncached(crop: Image.Image, thr: Optional[float] = None,
                       mid: Optional[float] = None) -> List[int]:
    g = crop.convert("L")
    m = min(g.size)
    if m <= 0:
        return []
    scale = max(1, int(math.ceil(72.0 / m)))
    if scale > 1:
        g = g.resize((g.width * scale, g.height * scale), Image.LANCZOS)
    arr = np.asarray(g).astype(np.uint8)
    if thr is None:
        thr = float(_otsu_threshold(arr))
        mid = 255.0
    votes: Dict[int, float] = {}
    vote_level: Dict[int, int] = {}
    order: List[int] = []

    def _vote(val: int, weight: float, level_idx: int) -> None:
        votes[val] = votes.get(val, 0.0) + weight
        vote_level[val] = max(vote_level.get(val, -1), level_idx)
        if val not in order:
            order.append(val)

    def _ranked() -> List[int]:
        # 票数优先；票数相同时取「更高阈值（笔画更粗、更保守）」的读数
        return sorted(order, key=lambda v: (-votes[v], -vote_level[v], order.index(v)))

    base_cfg = "--oem 3 --dpi 300 -c tessedit_char_whitelist=0123456789"

    def _scan(source: np.ndarray, levels: List[float], tag: str,
              level_base: int, weight: float) -> Optional[int]:
        """按多个阈值识别一遍，返回「最低阈值下 psm7 一次读通」的值（最高可信度）。"""
        for li, level in enumerate(levels):
            ink = source < level
            if ink.mean() > 0.85:
                break
            bw = Image.fromarray(np.where(ink, 0, 255).astype(np.uint8))
            bw = ImageOps.expand(bw, border=14, fill=255)
            bw.info["dpi"] = (300, 300)
            # 首选阈值多试几种模式，备用阈值只试最有效的两种（控制耗时）
            psms = (("--psm 7", "--psm 8", "--psm 13", "--psm 10", "--psm 6")
                    if li == 0 else ("--psm 8", "--psm 13"))
            for pi, extra in enumerate(psms):
                try:
                    # 一定要带超时：Tesseract 偶尔会挂住（尤其并发调用多时），
                    # 没超时的话整个脚本会静默卡死好几分钟（实测卡过 10 分钟）
                    text = pytesseract.image_to_string(bw, config=f"{base_cfg} {extra}",
                                                       timeout=OCR_TIMEOUT)
                except Exception:
                    text = ""
                nums = re.findall(r"\d+", text)
                if os.environ.get("MIKU_BOT_OCR_TRACE"):
                    print(f"   [ocr:{tag}] level={level:.0f} {extra} -> {text!r}")
                if len(nums) == 1 and 1 <= int(nums[0]) <= 99:
                    _vote(int(nums[0]), weight * (1.0 if (li == 0 and pi == 0) else 0.5),
                          level_base + li)
                    if li == 0 and pi == 0:
                        return int(nums[0])
                    break
        return None

    levels = [float(thr)]
    if mid is not None and mid > thr + 20:
        levels.append(thr + 0.30 * (mid - thr))
        levels.append(thr + 0.45 * (mid - thr))
        levels.append(thr + 0.60 * (mid - thr))

    result: List[int] = []
    hit = _scan(arr, levels, "raw", 0, 1.0)
    if hit is not None:
        result.append(hit)
    # 低对比度场景（游戏光标所在的高亮行/列是粉色条带，「13」容易被读成「15」）：
    # 把对比度拉满再识一遍，结果只作为「额外候选」，不覆盖上面的首选读数
    ac = np.asarray(ImageOps.autocontrast(Image.fromarray(arr))).astype(np.uint8)
    ac_thr = float(_otsu_threshold(ac))
    ac_levels = [ac_thr,
                 ac_thr + 0.30 * (255.0 - ac_thr),
                 ac_thr + 0.55 * (255.0 - ac_thr)]
    hit2 = _scan(ac, ac_levels, "ac", -20, 0.9)
    if hit2 is not None and hit2 not in result:
        result.append(hit2)
    for v in _ranked():
        if v not in result:
            result.append(v)
    return result


# --------------------------------------------------------------------------
# 棋盘几何 / 画面判断
# --------------------------------------------------------------------------



def _narrow_dip(prof: np.ndarray, k: int = 3) -> np.ndarray:
    """窄带「暗线」响应：比左右各 k 像素更暗的位置得到正值，能压掉大面积美术背景。"""
    if prof.shape[0] <= 2 * k + 2:
        return np.zeros_like(prof)
    p = np.pad(prof, k, mode="edge")
    return np.maximum(0.0, (p[:-2 * k] + p[2 * k:]) / 2.0 - p[k:-k])


def _line_positions(hp: np.ndarray, thr: float = 1.2) -> Tuple[np.ndarray, np.ndarray]:
    """
    把「窄带暗线响应」里超过阈值的连续像素合并成一条线。
    返回 (线位置, 线强度)：位置取加权重心，比直接取峰值稳（抗半像素抖动）。
    """
    idx = np.flatnonzero(hp > thr)
    if idx.size == 0:
        return np.zeros(0), np.zeros(0)
    groups = np.split(idx, np.flatnonzero(np.diff(idx) > 2) + 1)
    pos = np.empty(len(groups))
    stg = np.empty(len(groups))
    for i, g in enumerate(groups):
        w = hp[g]
        pos[i] = float((g * w).sum() / w.sum())
        stg[i] = float(w.max())
    return pos, stg


def _comb_fit(pos: np.ndarray, stg: np.ndarray, p: float, anchor: Optional[float] = None
              ) -> Optional[Tuple[float, np.ndarray, float]]:
    """
    按间距 p 拟合等距线阵，返回 (相位, 内点掩码, 最小二乘修正后的间距)。
    anchor 给出「某条线所在位置」时按它定相位，否则用最强的一批线做强度平方加权圆均值；
    之后反复「取内点 -> 最小二乘修 p」收敛，比枚举 offset 稳得多也快得多。
    """
    if p < 4.0 or pos.size < 3:
        return None
    # 容差放大到 2.2px：棋盘边框与深色背景相接时，线位置会被边缘对比带走 1~2px
    tol = min(2.2, max(0.7, 0.14 * p))
    if anchor is None:
        top = np.argsort(stg)[::-1][:min(pos.size, 30)]
        mask = np.zeros(pos.size, dtype=bool)
        mask[top] = True
    else:
        ph0 = float(anchor) % p
        mask = np.abs(((pos - ph0 + p / 2) % p) - p / 2) <= tol
        if int(mask.sum()) < 3:
            return None
    for _ in range(5):
        # 强度平方加权：5×5 棋盘只有 6 条线，背景杂峰数量远多于它，
        # 只用强度加权时相位会被成群的弱峰带偏，平方能让真正的网格线占主导
        ang = pos[mask] * (2 * math.pi / p)
        w = stg[mask] ** 2
        ph = (math.atan2(float((np.sin(ang) * w).sum()),
                         float((np.cos(ang) * w).sum())) * p / (2 * math.pi)) % p
        res = ((pos - ph + p / 2) % p) - p / 2
        mask = np.abs(res) <= tol
        if int(mask.sum()) < 3:
            return None
        k = np.round((pos[mask] - ph) / p)
        if np.unique(k).size < 3:
            return None
        vk = float(((k - k.mean()) ** 2).sum())
        if vk < 1e-6:
            break
        # 最小二乘微调：p = cov(k, x) / var(k)，相位取残差均值
        p = float(((k - k.mean()) * (pos[mask] - pos[mask].mean())).sum() / vk)
        if p < 4.0:
            return None
        ph = float(pos[mask].mean() - k.mean() * p) % p
    res = ((pos - ph + p / 2) % p) - p / 2
    inl = np.abs(res) <= tol
    if int(inl.sum()) < 3:
        return None
    return ph, inl, p


def _longest_run(ks: np.ndarray) -> Tuple[int, int]:
    """
    已排序的线索引里找最长的一段「索引连续」的线。
    要求严格连续：棋盘每一格都画了线，中间空一格说明那是提示区里的杂线，
    真正缺最外一条线的情况由 auto_detect_candidates 的候选变体兜底。
    """
    best = (0, 0)
    i, n = 0, int(ks.size)
    while i < n:
        j = i
        while j + 1 < n and int(ks[j + 1] - ks[j]) == 1:
            j += 1
        if (j - i) > (best[1] - best[0]):
            best = (i, j)
        i = j + 1
    return best


def _lattice_run(pos: np.ndarray, stg: np.ndarray, p: float, anchor: Optional[float] = None
                 ) -> Optional[Tuple[float, np.ndarray, float]]:
    """
    按间距 p 找出最长的一段等距线，返回 (得分, 这段线的下标, 修正后的间距)。
    得分 = 强度加权命中率：棋盘网格线的响应远强于背景美术与提示数字，
    所以「真的棋盘」会拿到接近 1 的分，杂点凑出来的线阵分很低。
    """
    fit = _comb_fit(pos, stg, p, anchor)
    if fit is None:
        return None
    ph, inl, p2 = fit
    ks_all = np.round((pos - ph) / p2).astype(int)
    keep: dict = {}
    for i in np.flatnonzero(inl):
        k = int(ks_all[i])
        if k not in keep or stg[i] > stg[keep[k]]:
            keep[k] = int(i)
    ks = np.array(sorted(keep))
    if ks.size < 4:
        return None
    a, b = _longest_run(ks)
    n = b - a + 1
    if n < 4:
        return None
    # 收紧两端：棋盘外的杂线（提示条带边缘、背景美术、下方文字）强度明显低，
    # 但可能恰好落在格线上把棋盘撑大；真被误删的最外一条线由候选变体兜底
    while n > 3:
        run_stg = [stg[keep[int(k)]] for k in ks[a:b + 1]]
        med = float(np.median(run_stg))
        if stg[keep[int(ks[a])]] < 0.5 * med:
            a += 1
            n -= 1
        elif stg[keep[int(ks[b])]] < 0.5 * med:
            b -= 1
            n -= 1
        else:
            break
    sel = np.array([keep[int(k)] for k in ks[a:b + 1]])
    total = float(stg.sum())
    hit = float(stg[sel].sum()) / total if total > 0 else 0.0
    return hit, sel, p2


def _scan_lattice(hp: np.ndarray, thr: float = 1.2, pmin: float = 8.0,
                  pmax: float = 260.0, min_score: float = 0.45
                  ) -> Optional[Tuple[float, float, float, float]]:
    """
    全自动搜索棋盘网格：返回 (格宽, 第一条线位置, 最后一条线位置, 平均强度)。
    先在线响应里找「线」，再在所有可能的等距组合里挑得分最高的一组，
    所以 5×5（只有 6 条线）这种小棋盘也能识别，格宽也不再限制在 10~60px。
    """
    pos, stg = _line_positions(hp, thr)
    if pos.size < 3:
        return None
    # 背景美术、提示条带边缘、提示数字的笔画都会产生弱响应，
    # 按最强线的比例裁掉，否则它们会把棋盘两端撑出一整格
    floor = max(thr, 0.3 * float(np.percentile(stg, 90)))
    strong = stg >= floor
    pos, stg = pos[strong], stg[strong]
    if pos.size < 3:
        return None
    cands = set()
    for d in np.diff(pos):
        if d < pmin or d > pmax:
            continue
        for k in (1, 2, 3, 4, 5, 6):      # 缺线时间距会是真间距的整数倍
            c = float(d) / k
            if pmin <= c <= pmax:
                cands.add(round(c, 2))
    if not cands:
        return None
    # 用最强的几条线当相位锚点各试一遍：5×5 的棋盘线少，光靠加权圆均值
    # 容易被成群的弱峰带偏（粗边框还会把一条线拆成两个峰）
    anchors: List[Optional[float]] = [None]
    for i in np.argsort(stg)[::-1][:min(pos.size, 6)]:
        anchors.append(float(pos[i]))
    best = None
    for p in sorted(cands):
        for anchor in anchors:
            got = _lattice_run(pos, stg, p, anchor)
            if got is None:
                continue
            score, sel, p2 = got
            if best is None or score > best[0] + 1e-9:
                best = (score, p2, sel)
    if best is None or best[0] < min_score:
        return None
    _, p, sel = best
    return p, float(pos[sel[0]]), float(pos[sel[-1]]), float(stg[sel].mean())


def _flatten_border(arr: np.ndarray) -> np.ndarray:
    """把客户区最外圈的窄边「抹平」，再去找网格线。

    画面最外圈常常是界面边框、标题/状态条、以及系统 OSD 覆盖层（FPS/CPU 那一长条），
    它们的上下边缘是非常强、非常长的直线，会被当成棋盘网格线，把等距拟合带偏——
    实测能把 5×5 棋盘认成 6×6，于是提示区域全部裁剪错位、读数变成垃圾。
    棋盘本身离客户区边缘还有一大截空档，抹掉最外圈不会碰到它。
    """
    h, w = arr.shape
    top = max(16, int(round(0.03 * h)))
    bottom = max(8, int(round(0.015 * h)))
    left = max(16, int(round(0.03 * w)))
    right = max(8, int(round(0.015 * w)))
    if h <= top + bottom + 8 or w <= left + right + 8:
        return arr
    out = arr.copy()
    out[:top, :] = out[top, :]
    out[h - bottom:, :] = out[h - bottom - 1, :]
    out[:, :left] = out[:, left][:, None]
    out[:, w - right:] = out[:, w - right - 1][:, None]
    return out


def auto_detect_candidates(img: Image.Image, limit: int = 4) -> List[Geometry]:
    """
    全自动定位棋盘：先在纵向找网格（提示条带会加强该方向信号），
    再限定在棋盘高度范围内找横向网格。格子数据实测线数算出并吸附到
    游戏支持的 5/10/20 标准尺寸，两端也各给一种「漏了最外一条线」的对齐方式。
    """
    arr = np.asarray(img.convert("L")).astype(np.float32)
    if arr.size == 0:
        return []
    arr = _flatten_border(arr)
    y_found = _scan_lattice(_narrow_dip(arr.mean(axis=1)))
    if y_found is None:
        return []
    p_y, y_lo, y_hi, _ = y_found
    band = arr[max(0, int(y_lo)):min(arr.shape[0], int(y_hi) + 1), :]
    if band.shape[0] < 20:
        return []
    x_found = _scan_lattice(_narrow_dip(band.mean(axis=0)))
    if x_found is None:
        return []
    p_x, x_lo, x_hi, _ = x_found

    def variants(p: float, lo: float, hi: float) -> List[Tuple[float, float, int]]:
        """实测线数优先吸附到游戏标准尺寸；再给出「两端各漏一条线」的变体。"""
        raw = int(round((hi - lo) / p))
        sizes: List[int] = []
        snapped = snap_size(raw, tol=1.0)
        if snapped != raw:
            sizes.append(snapped)
        sizes.append(raw)
        out: List[Tuple[float, float, int]] = []
        for n in sizes:
            if 3 <= n <= 30:
                out.append((lo, hi, n))                  # 实测首/末线
        for n in sizes:
            if not (3 <= n <= 30):
                continue
            for grow in (n + 1, snap_size(n + 1, tol=1.0)):
                if grow == n or not (3 <= grow <= 30):
                    continue
                out.append((lo - p, hi, grow))           # 可能漏了最前一条线
                out.append((lo, hi + p, grow))           # 可能漏了最后一条线
        return out

    cands: List[Geometry] = []
    for y0, y1, rows in variants(p_y, y_lo, y_hi):
        for x0, x1, cols in variants(p_x, x_lo, x_hi):
            cell_w, cell_h = (x1 - x0) / cols, (y1 - y0) / rows
            if cell_w < 6 or cell_h < 6:
                continue
            g = Geometry(x1=x0 + cell_w / 2, y1=y0 + cell_h / 2,
                         x2=x1 - cell_w / 2, y2=y1 - cell_h / 2, cols=cols, rows=rows)
            if all(abs(g.cell_w - c.cell_w) > 0.5 or abs(g.cell_h - c.cell_h) > 0.5
                   or g.rows != c.rows or g.cols != c.cols
                   or abs(g.x1 - c.x1) > 2 or abs(g.y1 - c.y1) > 2 for c in cands):
                cands.append(g)
    # 只保留游戏支持的方形尺寸（5×5 ~ 20×20），非方形候选（进关动画等）丢弃
    cands = [g for g in cands if g.rows == g.cols and g.rows in STANDARD_SIZES]
    return cands[:limit]



def detect_size_from_clicks(img: Image.Image, a: float, b: float, axis: str
                            ) -> Optional[Tuple[int, float, float, float]]:
    """
    手动校准用：已知左上/右下「格中心」a、b，推断格子数与格宽。
    返回 (n, p, 校正后的中心位置, 置信度)。对点击误差 ±6px 仍稳定。
    """
    arr = np.asarray(img.convert("L")).astype(np.float32)
    hp = _narrow_dip(arr.mean(axis=0) if axis == "x" else arr.mean(axis=1))
    L = hp.shape[0]
    span = b - a
    if span <= 10 or L < 20:
        return None
    cands = []
    for n in SQUARE_SIZES:
        if n < 2:
            continue
        p0 = span / (n - 1)
        if p0 < 8 or p0 > 200:
            continue
        best = None
        for p in np.arange(p0 * 0.97, p0 * 1.031, 0.1):
            lines = a - p / 2 + np.arange(n + 1) * p
            centers = a + np.arange(n) * p
            # 允许的滑动量用来吸收点击误差；上限 12px，避免大间距的粗格子“滑”到别的对齐上
            off_r = min(p / 5, 12.0)
            for off in np.arange(-off_r, off_r + 0.01, 0.25):
                li = np.clip(np.round(lines + off).astype(int), 0, L - 1)
                ci = np.clip(np.round(centers + off).astype(int), 0, L - 1)
                s = float(np.percentile(hp[li], 30) - np.percentile(hp[ci], 70))
                if best is None or s > best[0]:
                    best = (s, p, off)
        if best is not None:
            cands.append((best[0], n, best[1], a + best[2]))
    if not cands:
        return None
    cands.sort(reverse=True)
    top = cands[0][0]
    tol = max(1.0, 0.05 * abs(top))
    # 粗间距的格子往往是细网格 3 倍整数倍的「别名」，分数接近时选格子更多的那组
    s, n, p, a2 = max((c for c in cands if c[0] >= top - tol), key=lambda c: c[1])
    return n, p, a2, s


def snap_size(n: int, tol: float = 0.34) -> int:
    for std in STANDARD_SIZES:
        if abs(n - std) <= tol:
            return std
    return n


def board_present(screen: Screen, geom: Geometry) -> Tuple[bool, str]:
    """判断当前画面是不是「某一关的棋盘」（菜单/结算、过关图都会返回 False）。"""
    img = screen.grab_client()
    arr = np.asarray(img.convert("L")).astype(np.float32)
    if arr.size == 0:
        return False, "空画面"
    y0 = max(0, int(geom.y1 - geom.cell_h / 2))
    y1 = min(arr.shape[0], int(geom.y2 + geom.cell_h / 2) + 1)
    x0 = max(0, int(geom.x1 - geom.cell_w / 2))
    x1 = min(arr.shape[1], int(geom.x2 + geom.cell_w / 2) + 1)
    details = []
    ok_axes = 0
    for axis in ("x", "y"):
        if axis == "x":
            prof = arr[y0:y1, :].mean(axis=0)
            first, count, p = geom.x1, geom.cols, geom.cell_w
        else:
            prof = arr[:, x0:x1].mean(axis=1)
            first, count, p = geom.y1, geom.rows, geom.cell_h
        hp = _narrow_dip(prof)
        L = hp.shape[0]
        lines = np.clip(np.round(first - p / 2 + np.arange(count + 1) * p).astype(int), 0, L - 1)
        mids = np.clip(np.round(first + np.arange(count) * p).astype(int), 0, L - 1)
        s = float(np.percentile(hp[lines], 30))
        m = float(np.percentile(hp[mids], 70))
        details.append(f"{axis}: 线={s:.2f} 格心={m:.2f}")
        if s > 2.0 and s > m + 1.0:
            ok_axes += 1
    return ok_axes >= 2, "；".join(details)


# --------------------------------------------------------------------------
# 识别提示数字
# --------------------------------------------------------------------------


@dataclass
class Puzzle:
    rows: List[List[int]]
    cols: List[List[int]]
    rows_n: int
    cols_n: int

    @property
    def row_sums(self) -> List[int]:
        return [sum(c) for c in self.rows]

    @property
    def col_sums(self) -> List[int]:
        return [sum(c) for c in self.cols]

    def key(self):
        return (tuple(tuple(c) for c in self.rows), tuple(tuple(c) for c in self.cols))


def _band_levels(band: np.ndarray) -> Optional[Tuple[float, float]]:
    """由条带估计（墨迹分界阈值, 底色亮度）；返回 None 表示这条线上没有数字。"""
    if band.size < 4:
        return None
    flat = band.ravel()
    # 底色基准用 80 分位（面板本身的亮色），避免条带里混入的深色背景/美术把阈值带偏
    mid = float(np.percentile(flat, 80))
    cand = flat[flat < mid - 25]
    if cand.size < max(20.0, 0.002 * flat.size):
        return None
    ink = float(np.percentile(cand, 10))
    if mid - ink < 25:
        return None
    return (ink + mid) / 2.0, mid


def _band_threshold(band: np.ndarray) -> Optional[float]:
    lv = _band_levels(band)
    return None if lv is None else lv[0]


def _blank_line_edges(band: np.ndarray, thr: float) -> np.ndarray:
    """
    抹掉条带两端「整列都暗」的边框线（棋盘外框/提示面板边框）。
    只把边缘列涂成亮色，不改动数组尺寸，后续坐标仍然一致。
    """
    dark = band < thr
    h = dark.shape[0]
    need = max(3, int(0.5 * h))
    x0, x1 = 0, dark.shape[1] - 1
    while (x1 - x0) > 8 and dark[:, x1].sum() >= need:
        x1 -= 1
    while (x1 - x0) > 8 and dark[:, x0].sum() >= need:
        x0 += 1
    if x0 == 0 and x1 == dark.shape[1] - 1:
        return band
    out = band.copy()
    out[:, :x0] = 255.0
    out[:, x1 + 1:] = 255.0
    return out


def _digit_blobs(band: np.ndarray, gap_max: int = 7, thr: Optional[float] = None,
                 min_w: int = 0
                 ) -> List[Tuple[int, int, int, int, int]]:
    """
    在一条提示条带里找数字块。
    band: 灰度二维数组，列方向 = 数字排列方向（远离棋盘 -> 靠近棋盘）。
    先用 2x2 腐蚀去掉 1 像素宽的细线（游戏高亮条的描边），再按「沿排列方向的
    连续笔画段」切块，允许小幅间隙合并（两位数会被合并成一个数字）。
    返回 [(列起, 列止, 行起, 行止, 暗像素数)]

    min_w>0 时：**在合并之前**把宽度小于 min_w 的墨迹段所在列抹掉。
    用途：提示区边缘会漏进来一条装饰竖条（实测 3px，而真实「1」约 5px），
    它会并进槽位、把整块裁框撑大，于是 OCR 把「7」读成「17」这类两位数、
    候选炸成 10~19 一片（Lv3-016 现场实锤）。
    ⚠ **只对行方向（axis=="R"）开启**：列方向同类过滤实测会把列读塌（324→304），
    因为列提示区里的细线是横向分隔线、属于正常结构。
    """
    if band.size < 4:
        return []
    if thr is None:
        thr = _band_threshold(band)
        if thr is None:
            return []
    dark = band < thr
    dense = np.zeros_like(dark)
    dense[:-1, :-1] = dark[:-1, :-1] & dark[1:, :-1] & dark[:-1, 1:] & dark[1:, 1:]
    colcnt = dense.sum(axis=0)
    if min_w > 0:
        rawcnt = dark.sum(axis=0)      # 腐蚀前宽度：真实「1」≈5px、装饰竖条 3px
        rs = None
        for x in range(rawcnt.shape[0] + 1):
            v = rawcnt[x] if x < rawcnt.shape[0] else 0
            if v >= 2:
                if rs is None:
                    rs = x
            elif rs is not None:
                if x - rs < min_w:
                    colcnt[rs:x] = 0
                rs = None

    # ⚠ 2026-09-30 试过在这里把"太细的竖条"所在列抹掉（想让它不并进槽位）——
    #   **实测有害**：行和 326→328、列和 324→304，列方向反而塌了。
    #   前后一共试过四种"细竖条"处理（切块前抹列 / 切块后过滤 / 收窄"够窄就当1" /
    #   给"补十位"加上下界），**每一种都是拆东墙补西墙**。
    #   ⇒ 结论：病根不在"细不细"，而在**槽位框（box）的合成方式**：
    #     幽灵与真实数字被 gap_max 合并成同一个槽位、撑大了整块裁框。
    #     下一步应当直接量每条线的原始墨迹段分布、按"段"重建槽位框，
    #     而不是继续在细宽阈值上打转。（保留原始代码，见 git 历史与 build/versions 快照）

    runs: List[Tuple[int, int]] = []
    start, gap, last_ok = None, 0, 0
    for x in range(colcnt.shape[0]):
        if colcnt[x] >= 2:
            if start is None:
                start = x
            gap = 0
            last_ok = x
        elif start is not None:
            gap += 1
            if gap > gap_max:
                runs.append((start, last_ok))
                start = None
    if start is not None:
        runs.append((start, last_ok))

    out = []
    for x0, x1 in runs:
        seg = dense[:, x0:x1 + 1]
        rows = np.where(seg.any(axis=1))[0]
        if rows.size < 3:
            continue
        p0, p1 = int(rows[0]), int(rows[-1])
        out.append((x0, x1, p0, p1, int(seg[p0:p1 + 1, :].sum())))
    return out


def _enclosed_holes(mask: np.ndarray) -> List[Tuple[int, float, float, int, int]]:
    """
    数出 mask（True=墨迹）里「被墨迹完全围住」的空洞（背景走 4 邻域、墨迹走 8 邻域）。
    返回 [(像素数, 中心纵向比, 中心横向比, 高, 宽)]，两个比都是相对 mask 尺寸的 0~1。
    """
    h, w = mask.shape
    pad = np.zeros((h + 2, w + 2), dtype=bool)
    pad[1:-1, 1:-1] = mask
    bg = ~pad
    seen = np.zeros_like(bg)
    hh, ww = bg.shape
    out: List[Tuple[int, float, float, int, int]] = []
    for y in range(hh):
        for x in range(ww):
            if not bg[y, x] or seen[y, x]:
                continue
            seen[y, x] = True
            stack = [(y, x)]
            comp: List[Tuple[int, int]] = []
            touch = False
            while stack:
                cy, cx = stack.pop()
                comp.append((cy, cx))
                if cy in (0, hh - 1) or cx in (0, ww - 1):
                    touch = True            # 连到边界 → 是外部背景，不是孔
                for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                    if 0 <= ny < hh and 0 <= nx < ww and bg[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if touch or not comp:
                continue
            ys = [p[0] - 1 for p in comp]
            xs = [p[1] - 1 for p in comp]
            out.append((len(comp),
                        ((min(ys) + max(ys)) / 2.0) / max(1, h - 1),
                        ((min(xs) + max(xs)) / 2.0) / max(1, w - 1),
                        max(ys) - min(ys) + 1, max(xs) - min(xs) + 1))
    return out


def _is_blank_zero(glyph: np.ndarray, mid: float) -> bool:
    """
    这块墨迹是不是游戏画的「本行/列没有填色块」的 0？

    游戏对空行/空列的提示位画的是一个**灰色的 0**（不是数字），它比真正的提示数字浅
    一大截，而这个字号（约 11×15 像素）下 Tesseract 会把它读成 8 或 6 —— 实测真机
    Lv1-04：5 个空行提示被读成 8/8/8、2 个空列被读成 6/8，行提示总和 50 ≠ 列提示总和
    40（真值都是 26），连读 3 帧全判死，这一关再也读不出来。

    不能靠「深浅」判它（被划掉的提示数字和它是同一个灰），所以这里量的是**字形拓扑**：
    一个闭合孔、孔够大、两个方向都撑得起字形的一半左右、且孔在字形正中。实测区分度——
      0：孔 53~59 像素，孔 10×6 / 字形 15×11，孔中心 0.50~0.55
      4：孔 3 像素；6：孔 17~19 像素、偏下（中心 0.73~0.75）；8：两个孔（9+5 像素）
    阈值在几个「明显比底色暗」的档位上各试一次：灰字在不同底色上（白/浅青/光标粉色
    条）会被二值化成粗细不同的环，只要有一档看得出「一个大孔」就认它是 0。
    """
    h, w = glyph.shape
    if h < 7 or w < 5:
        return False
    gmin = float(glyph.min())
    for level in (mid - 25.0, mid - 45.0, mid - 70.0):
        if level <= gmin + 8.0:
            continue                    # 这一档已经取不到墨迹了
        mask = glyph < level
        frac = float(mask.mean())
        if not 0.15 <= frac <= 0.70:
            continue                    # 太稀（笔画断成点）或太满（糊成一块）都不判
        holes = _enclosed_holes(mask)
        if len(holes) != 1:
            continue
        n, cy, cx, hh, ww = holes[0]
        # 孔要「又大又居中」：面积占字形一成以上、两个方向都撑得起字形的一半左右、
        # 中心两种方向都在正中一带。0 实测：53~59 像素（占字形 0.32~0.36）/
        # 占满 0.4~0.67 尺寸 / 中心 0.50~0.55。面积按比例算，别写死像素数——
        # 25×25 棋盘那一档字只有 8 像素高，写死 25 像素会把真 0 放过去。
        if (n >= max(10.0, 0.12 * h * w) and hh >= 0.4 * h and ww >= 0.4 * w
                and 0.35 <= cy <= 0.65 and 0.35 <= cx <= 0.65):
            return True
    return False


def _digit_side(crop: Image.Image, mid: float) -> Optional[int]:
    """
    看字形上半截的墨迹偏左还是偏右，用来给「3 / 5」这类摇摆不定的读数定夺。

    这个字体里 5 的顶横是**靠左边一竖**接下来的、3 是靠右边一条弧接下来的：取字形高度
    20%~40% 那几行（顶横之后、中间那道横之前），量左右半区的墨迹占比。实测（15 个样本、
    两张真机图 + 一张历史图）：5 → 左占比 0.69~0.91，3 → 0.28~0.60。特征**不够明显时
    一律返回 None**（宁可不插嘴，也不要拿模糊的字形去覆盖 OCR 的读数）。

    crop 是图像方向的字形小图（列轴 = 字形的左右），所以行提示、列提示都能直接用，
    不用管条带里有没有转置。
    """
    arr = np.asarray(crop.convert("L")).astype(np.float32)
    mask = arr < (mid - 25.0)
    ys, xs = np.where(mask)
    if ys.size < 8:
        return None
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    h, w = y1 - y0, x1 - x0
    if w < 6 or h < 10:
        return None
    a = y0 + int(round(0.20 * h))
    b = y0 + max(int(round(0.40 * h)), a + 1)
    band = mask[a:b, x0:x1]
    mid_col = w // 2
    if band.size == 0 or mid_col <= 0:
        return None
    left = float(band[:, :mid_col].mean())
    right = float(band[:, mid_col:].mean())
    if left + right <= 0:
        return None
    frac = left / (left + right)
    if frac >= 0.68:
        return 5
    if frac <= 0.32:
        return 3
    return None


USE_CLUE_ZONE_CLAMP = False   # 见下方 _clue_zone_far_edge：试过，读数没变好，默认关


# --------------------------------------------------------------------------
# 字形库：把"游戏确认过"的线裁出来的数字当模板，用它给每个提示数字做独立判读。
#
# 为什么需要：2026-09-30 实测发现，小字形上**原始 OCR 会大面积给出垃圾值**
# （20×20 的题里出现 32/62/78 这种不可能的提示），而纠错机制会从候选里挑一个
# "看起来合理"的拼成读数 —— 结果**自洽、可解、唯一解，但整份题面是拼出来的**，
# 所有下游检查（总和/可解性/图案比对）都拿它核对自身，永远抓不到。
#
# 库怎么来：`debug/probe_glyph_lib2.py` 用脚本自己的 [pick] 决策给裁图打标签，
# 逐线自证（各槽值能还原该线已知提示数字），只用游戏已变灰（确认读数正确）的线。
# 用前必须按墨迹**收紧裁框**：读者的框带 padding（同一个"1"能是 16~28px 宽），
# 收紧后同一数字跨帧相似度 0.91~1.00、不同数字之间只有 0.32~0.42。
# --------------------------------------------------------------------------
GLYPH_LIB_PATH: Path = DEBUG_DIR / "glyph_lib.npz"
GLYPH_LIB_ON = not bool(os.environ.get("MIKU_BOT_NO_GLYPH_LIB"))   # 可用环境变量关掉
GLYPH_LIB_CONF = 0.72        # 判读所需的最低相似度
GLYPH_LIB_MARGIN = 0.10      # 首选与次选的最小分差
_GLYPH_LIB_CACHE: Optional[Tuple[list, list]] = None


def _glyph_lib() -> Optional[Tuple[list, list]]:
    """懒加载字形库；失败（文件不存在等）返回 None，调用方退化为纯 OCR。"""
    global _GLYPH_LIB_CACHE
    if _GLYPH_LIB_CACHE is not None:
        return _GLYPH_LIB_CACHE or None
    try:
        z = np.load(GLYPH_LIB_PATH, allow_pickle=True)
        labels = [int(v) for v in z["labels"]]
        imgs = list(z["imgs"])
        _GLYPH_LIB_CACHE = (labels, imgs)
        return labels, imgs
    except Exception:
        _GLYPH_LIB_CACHE = ([], [])
        return None


def _tight(mask: np.ndarray) -> Optional[np.ndarray]:
    """按自身墨迹收紧裁框（跨帧可比的关键）。"""
    ys = np.where(mask.any(axis=1))[0]
    xs = np.where(mask.any(axis=0))[0]
    if len(ys) == 0 or len(xs) == 0:
        return None
    return mask[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1]


def _glyph_score(g1: np.ndarray, g2: np.ndarray) -> float:
    """底对齐 + 居中 + ±2px 平移搜索取最大 IoU，再**按面积比惩罚**。

    没有面积惩罚的话，大模板（如两位数的"13"）会因为覆盖面积大而在任何输入上都拿高分
    —— 实测这就是"不管给什么图都判 13"的根源。
    """
    h = max(g1.shape[0], g2.shape[0])
    w = max(g1.shape[1], g2.shape[1]) + 4
    best = 0.0

    def place(g):
        c = np.zeros((h + 2, w), np.uint8)
        dy = h - g.shape[0]
        dx = (w - g.shape[1]) // 2
        c[dy:dy + g.shape[0], dx:dx + g.shape[1]] = g
        return c

    c1 = place(g1)
    c2b = place(g2)
    for sy in (-2, -1, 0, 1, 2):
        for sx in (-2, -1, 0, 1, 2):
            c2 = np.roll(np.roll(c2b, sy, axis=0), sx, axis=1)
            inter = int((c1 & c2).sum())
            union = int((c1 | c2).sum())
            if union:
                best = max(best, inter / union)
    a1, a2 = int(g1.sum()), int(g2.sum())
    size_ratio = (min(a1, a2) / max(a1, a2)) if max(a1, a2) else 0.0
    return best * size_ratio


def glyph_read(crop_l: np.ndarray) -> Optional[int]:
    """用一个槽位的裁图（灰度）查询字形库：可信则返回数值，否则 None（=读不出）。"""
    lib = _glyph_lib()
    if not lib:
        return None
    labels, imgs = lib
    mask = (crop_l < 195).astype(np.uint8)
    mask = _tight(mask)
    if mask is None or mask.size == 0:
        return None
    h, w = mask.shape
    best_per_label: Dict[int, float] = {}
    for s, lab in zip(imgs, labels):
        # 尺寸差太多不可能是同一个字（不缩放，只筛）——这一步同时是**主要的提速手段**：
        # 库里的字形尺寸很集中，先按尺寸筛掉绝大多数模板再算 IoU。
        if abs(s.shape[0] - h) > 6 or abs(s.shape[1] - w) > 6:
            continue
        sc = _glyph_score(mask, s)
        if sc > best_per_label.get(lab, 0.0):
            best_per_label[lab] = sc
    if not best_per_label:
        return None
    ranked = sorted(best_per_label.items(), key=lambda kv: -kv[1])
    top = ranked[0]
    margin = top[1] - ranked[1][1] if len(ranked) >= 2 else 1.0
    if top[1] >= GLYPH_LIB_CONF and margin >= GLYPH_LIB_MARGIN:
        return int(top[0])
    return None                            # 不确定 → 读不出（绝不让上层去猜）


def _clue_zone_far_edge(arr: np.ndarray, geom: Geometry, axis: str,
                        border: float, max_slots: int) -> float:
    """
    找「真正有提示数字」的那一段的**远边**（行提示=左边界，列提示=上边界）。
    ------------
    `max_col_slots` / `max_row_slots` 默认取 `(rows+1)//2`（20×20 → 10），那是**理论上限**
    （一格里穿插 1 个空格时确实能有 10 个数字），但游戏实际画的提示区只有**真实数字个数**
    那么高。拿理论上限去开裁框，会往棋盘外多伸 6~7 格，**盖上游戏的界面元素**——
    2026-09-30 凌晨 Lv3-028 就是这么把界面上一个灰蓝色的「1」读进了列15
    （真值 [2,3,2] 被读成 [1,2,3,2]），题意错 → 解出来的图案整片错位 → 行13/列14 永远不变灰。

    判据
    ----
    游戏的提示数字是**紧贴棋盘**堆叠的，数字之间只有很小的行距、没有成片空白；
    界面元素与最外那个数字之间则会有一条明显空白带。所以：从棋盘边往远处走，
    遇到第一条「连续 ≥0.6 格宽的空白」就停，那里就是提示区的远边。
    """
    if axis == "C":
        # 列提示：横向铺满整个棋盘宽度，纵向从棋盘边往上找
        x0 = max(0, int(geom.x1 - geom.cell_w / 2))
        x1 = min(arr.shape[1], int(geom.x2 + geom.cell_w / 2))
        y_far = max(0, int(border - max_slots * geom.cell_h))
        y_near = min(arr.shape[0], int(border))
        if x1 - x0 < 4 or y_near - y_far < 4:
            return 0.0
        prof = (arr[y_far:y_near, x0:x1] < 195).sum(axis=1)   # 每行的墨迹像素数
        # 阈值取 195：既算黑字（≈64），也算游戏已经变灰的字（≈155~180），
        # 但不算提示区的浅绿/白底（≈205~250）。这样"涂了一半"的帧也能正确定位。
        # 注意：棋盘与提示区之间本来就有一条空白边距，**必须先在空白之后见到墨迹**，
        # 再从墨迹里遇到成片空白才算"提示区结束"，否则第一步就会把整段切掉
        # （2026-09-30 实测踩过：列8 被切坏，读出 46/62 这种越界值）。
        blank_need = max(4, int(round(0.6 * geom.cell_h)))
        run, seen_ink = 0, False
        for k in range(len(prof) - 1, -1, -1):                 # 从棋盘边往远处走
            if prof[k] < 2:
                run += 1
                if seen_ink and run >= blank_need:
                    return float(y_far + k + run)
            else:
                seen_ink = True
                run = 0
        return float(y_far)
    # 行提示：纵向单行，横向从棋盘边往左找
    y0 = max(0, int(geom.y1 - geom.cell_h / 2))
    y1 = min(arr.shape[0], int(geom.y2 + geom.cell_h / 2))
    x_far = max(0, int(border - max_slots * geom.cell_w))
    x_near = min(arr.shape[1], int(border))
    if y1 - y0 < 4 or x_near - x_far < 4:
        return 0.0
    prof = (arr[y0:y1, x_far:x_near] < 195).sum(axis=0)
    blank_need = max(4, int(round(0.6 * geom.cell_w)))
    run, seen_ink = 0, False
    for k in range(len(prof) - 1, -1, -1):
        if prof[k] < 2:
            run += 1
            if seen_ink and run >= blank_need:
                return float(x_far + k + run)
        else:
            seen_ink = True
            run = 0
    return float(x_far)


def _read_clue_axis(img: Image.Image, geom: Geometry, axis: str,
                    workers: int = 8, boxes: Optional[List[tuple]] = None
                    ) -> Tuple[Dict[int, List[int]], List[tuple]]:
    """...
    读取一侧提示数字。axis='R' 读行提示（棋盘左侧），'C' 读列提示（棋盘上方）。
    数字布局按「紧贴棋盘右侧/下侧对齐」处理，不假设与棋盘格对齐。
    """
    # 排障用：MIKU_BOT_OCR_WORKERS=1 强制单线程 —— 多线程时 OCR 的候选集带随机性，
    # 同一帧两次读出来的结果可能不同（实测 326/324 与 326/304 都出现过），
    # 做 A/B 对比必须先固定成单线程，否则改了什么、有没有用根本看不出来。
    _envw = os.environ.get("MIKU_BOT_OCR_WORKERS")
    if _envw:
        try:
            workers = max(1, int(_envw))
        except ValueError:
            pass
    arr = np.asarray(img.convert("L")).astype(np.float32)
    H, W = arr.shape
    if axis == "R":
        border = geom.x1 - geom.cell_w / 2.0
        boss, perp_cell = geom.rows, geom.cell_h
        perp_center = lambda i: geom.y1 + i * geom.cell_h
        pitch_hint, max_slots = geom.cell_w, geom.max_row_slots
    else:
        border = geom.y1 - geom.cell_h / 2.0
        boss, perp_cell = geom.cols, geom.cell_w
        perp_center = lambda i: geom.x1 + i * geom.cell_w
        pitch_hint, max_slots = geom.cell_h, geom.max_col_slots
    half = perp_cell * 0.45
    # 合并间隙：行提示的数字是横排的，两位数的字间距要合并；
    # 列提示的数字是上下堆叠的，间隔本身很小，只能容忍极小间隙。
    gap_ratio = 0.32 if axis == "R" else 0.16
    gap_max = max(4, int(round(gap_ratio * pitch_hint)))
    # ★ 提示区的真实远边：默认的 max_slots（(rows+1)//2，20×20 时是 10）只是理论上限，
    #   按它开裁框会伸进游戏界面，把界面上的数字读成提示（Lv3-028 列15 的假「1」）。
    #   —— 但 2026-09-30 凌晨实测：这个钳制**没能让读数变好**（列和 193→196、仍不自洽），
    #   按「不留没收益的改动」的规矩先**默认关掉**，代码与结论留在案上，等找到更稳的判据再启用。
    zone_far = _clue_zone_far_edge(arr, geom, axis, border, max_slots) \
        if USE_CLUE_ZONE_CLAMP else 0.0

    # ⚠ 2026-09-30 试过"抹掉提示面板竖边线"（判据：某列在≥80%的行里都有墨迹）。
    #   **实测有害**：行和 326→195/182。原因有两个，都记在这里免得再试：
    #   ① 统计范围必须先限定在提示条带内，否则棋盘自身竖网格线（每行都有）被整片抹掉；
    #   ② **就算范围对了也不成立** —— 提示数字是**贴着棋盘右对齐**的，所以"右端某一列"
    #      在大多数行里都落在某个数字内部，覆盖率和幽灵（x800..807，18/20 行）一样高，
    #      抹掉就等于横切过所有数字。**"跨行覆盖率"这个判据在本游戏的排版下无效。**
    #   至此共四种"按宽度/间隙/覆盖率分辨幽灵"的方案全部实测失败（见 README）。

    def collect(strip_w: float, strict: bool, gap: int, far_pad: float = 0.0
                ) -> List[List[tuple]]:
        """在给定条带宽度下收集每行/列的候选数字块。

        far_pad 只在「远离棋盘」的一侧（行=左、列=上）额外放宽条带：提示数字是
        紧贴棋盘对齐排布的，最左/最上的那个离棋盘最远、离条带边界最近；若它是
        两位数，条带边界可能把它截断（a0<=0 会被当成界面元素丢掉）。
        但放宽**不能越过 zone_far**（提示区真实远边），否则会读到界面上去了。
        """
        out: List[List[tuple]] = []
        for i in range(boss):
            c = perp_center(i)
            if axis == "R":
                x0 = max(0, int(border - strip_w - far_pad))
                x0 = max(x0, int(zone_far))
                x1 = min(W, max(1, int(border - 3)))    # 不把棋盘边框线算进来
                y0 = max(0, int(c - half))
                y1 = min(H, int(c + half) + 1)
                band = arr[y0:y1, x0:x1]
            else:
                y0 = max(0, int(border - strip_w - far_pad))
                y0 = max(y0, int(zone_far))
                y1 = min(H, max(1, int(border - 3)))
                x0 = max(0, int(c - half))
                x1 = min(W, int(c + half) + 1)
                band = arr[y0:y1, x0:x1].T   # 转置后列方向 = 纵向（上 -> 下）
            items: List[Tuple[float, tuple, int, float, float, List[tuple], bool]] = []
            lv = _band_levels(band)
            if lv is None:
                out.append(items)
                continue
            thr, mid = lv
            along_len = band.shape[1]
            if strict:
                band = _blank_line_edges(band, thr)
            # 行提示：垂直方向是字高；列提示：垂直方向是字宽（可能只有 2~3 像素）
            min_perp = perp_cell * 0.25 if axis == "R" else 2.0
            blobs = _digit_blobs(band, gap, thr)
            # 游戏会把「已经满足」的那几个提示数字**逐个**染成浅灰（比黑字浅一大截）。
            # 只用黑字阈值找墨迹，这些灰数字会整块漏掉：轻则「行和 115 ≠ 列和 62」整帧
            # 判死、这一关再也读不出来（读失败 → 重置 → 又读失败），重则漏掉的那几个数字
            # 恰好凑出一个能解但错的题，照着它填就会填错格子。
            # 这里再按「明显比底色暗」的宽松阈值兜一遍，只补严格阈值没覆盖到的墨迹块；
            # 读数本身仍走多阈值投票，不受影响。
            light_thr = mid - 25.0          # 与 _band_levels 里「墨迹候选」同一口径
            if light_thr > thr + 12.0:
                found = [(b[0], b[1]) for b in blobs]
                extra = []
                for blob in _digit_blobs(band, gap, light_thr):
                    a0, a1 = blob[0], blob[1]
                    if any(not (a1 < f0 or a0 > f1) for f0, f1 in found):
                        continue            # 与已有块重叠 → 同一团墨迹
                    found.append((a0, a1))
                    extra.append(blob)
                if extra:
                    blobs = sorted(blobs + extra, key=lambda b: b[0])
            for a0, a1, p0, p1, dark in blobs:
                w, h = a1 - a0 + 1, p1 - p0 + 1
                if w > max(14.0, 1.0 * pitch_hint) or w < 2:
                    continue
                if h > perp_cell * 1.05 or h < max(3.0, min_perp):
                    continue
                if strict and (a0 <= 0 or a1 >= along_len - 2):
                    continue          # 贴在搜索条带边缘 → 是棋盘外被截断的界面元素
                if strict and w > 0.6 * pitch_hint and h > 0.6 * perp_cell:
                    continue          # 两个方向都很大 → 界面元素而非数字
                if dark < 8:
                    continue
                # 空行/空列的提示位画的是一个灰色的 0（见 _is_blank_zero）：它会被 OCR 读成
                # 8/6，一旦当成数字就把「行和 = 列和」这条硬约束顶掉（实测整帧判死、这关再也
                # 读不出来）。按字形认出来，标成「空提示」：**留在 items 里参与下面的链式筛选**
                # （它就是这条线上离棋盘最近的那块，抽掉它会把更远的界面美术当成数字），
                # 到真正建 OCR 任务时再丢掉。
                # 两道防线，免得把真提示标错：
                #   1) 这条线上除了它再没有别的墨迹块——空行/空列本来就只有这个 0；两位数
                #      （10~15）的「1」就贴在它旁边一格内，会被这条挡掉；
                #   2) 块宽不超过一格（两位数是一整块，比一格宽）。
                # 只在收紧条带那一遍判（另一遍是量提示间距用的，那时还没算出 pitch）。
                alone = blank_zero = False
                if strict:
                    alone = all(abs((b[0] + b[1]) / 2.0 - (a0 + a1) / 2.0) > 1.2 * pitch
                                for b in blobs if (b[0], b[1]) != (a0, a1))
                    blank_zero = (alone and w <= 0.6 * pitch_hint
                                  and _is_blank_zero(band[p0:p1 + 1, a0:a1 + 1], mid))
                if os.environ.get("MIKU_BOT_TRACE"):
                    print(f"    [blob] {axis}{i} 沿[{a0},{a1}] w={w} h={h} dark={dark} "
                          f"最暗={float(band[p0:p1 + 1, a0:a1 + 1].min()):.0f} "
                          f"块数={len(blobs)} 独占={alone} 空0={blank_zero}")
                if axis == "R":
                    pos = x0 + (a0 + a1) / 2.0
                    box = (x0 + a0, y0 + p0, x0 + a1 + 1, y0 + p1 + 1)
                else:
                    pos = y0 + (a0 + a1) / 2.0
                    box = (x0 + p0, y0 + a0, x0 + p1 + 1, y0 + a1 + 1)
                # 两位数（「14」「11」「12」）的识别：把块按「零间隙」再拆一次，
                # 拆出 2~3 个子块就分别识别后拼接。
                # 行提示的数字沿排列方向排开；列提示的数字是左右并排的，要沿水平方向拆。
                subs: List[tuple] = []
                if not blank_zero and w > 0.3 * pitch_hint:
                    if axis == "R":
                        ba = max(0, a0 - 1)
                        sblobs = _digit_blobs(band[:, ba:a1 + 2], 0, thr)
                        if 2 <= len(sblobs) <= 3:
                            for sa0, sa1, sp0, sp1, _ in sblobs:
                                ga0, ga1 = ba + sa0, ba + sa1
                                if ga1 - ga0 + 1 < 2:
                                    subs = []
                                    break
                                subs.append((x0 + ga0, y0 + sp0, x0 + ga1 + 1, y0 + sp1 + 1))
                    else:
                        ba, bp = max(0, a0 - 1), max(0, p0 - 1)
                        sblobs = _digit_blobs(band[bp:p1 + 2, ba:a1 + 2].T, 0, thr)
                        if 2 <= len(sblobs) <= 3:
                            for sa0, sa1, _sp0, _sp1, _ in sblobs:
                                gp0, gp1 = bp + sa0, bp + sa1
                                if gp1 - gp0 + 1 < 2:
                                    subs = []
                                    break
                                subs.append((x0 + gp0, y0 + ba, x0 + gp1 + 1, y0 + a1 + 1))
                items.append((pos, box, dark, thr, mid, subs, blank_zero))
            # 提示数字是紧贴棋盘连续排列的；离得太远（隔了空位）的多半是界面元素，
            # 比如窗口顶部被误当成数字的图标（会让该列/行多出错误提示）
            if strict and len(items) > 1:
                items.sort(key=lambda it: it[0], reverse=True)
                keep = [items[0]]
                for idx, it in enumerate(items[1:], start=1):
                    # 变量不能叫 gap：gap 是本函数的形参（数字块合并间隙），在这里被覆盖会
                    # 污染「之后每一条线」的墨迹合并——相邻数字被并成一个宽块后当界面元素丢掉，
                    # 结果除第一条线外全都读不到数字。
                    hop = keep[-1][0] - it[0]
                    # 链尾（离棋盘最远）那一跳放宽：它若为两位数，中心会比一格间距
                    # 更偏外（两位墨迹更宽），用 1.7 倍阈值容易被误判成界面元素而
                    # 连同它一起把整条提示丢掉。
                    limit = pitch * 2.35 if idx == len(items) - 1 else pitch * 1.7
                    if hop <= limit:
                        keep.append(it)
                    else:
                        break
                items = keep
            out.append(items)
        return out

    # 第一遍：用较宽的条带量出「提示格间距」（游戏里约 0.85 格宽，与棋盘格不对齐）
    probe = collect(max_slots * pitch_hint * 1.15, strict=False, gap=gap_max)
    est: List[float] = []
    for items in probe:
        if not items:
            continue
        pos = sorted(it[0] for it in items)
        est.append(2.0 * (border - pos[-1]))
        est.extend(pos[i + 1] - pos[i] for i in range(len(pos) - 1))
    est = [v for v in est if pitch_hint * 0.5 <= v <= pitch_hint * 1.1]
    pitch = float(np.median(est)) if len(est) >= 3 else pitch_hint
    pitch = float(np.clip(pitch, pitch_hint * 0.55, pitch_hint * 1.2))
    # 第二遍：条带收紧到真实提示面板范围（面板高/宽 = 最大提示数 × 间距），
    # 避免把棋盘外的界面元素当成数字
    strip_w = (max_slots + 0.15) * pitch
    gap_use = max(4, int(round(gap_ratio * pitch)))
    # 远离棋盘一侧再多留一个提示格，保护最左/最上那个提示不被条带边界截断
    raw = collect(strip_w, strict=True, gap=gap_use, far_pad=pitch)
    if os.environ.get("MIKU_BOT_TRACE"):
        print(f"[trace {axis}] border={border:.1f} pitch={pitch:.1f} 条带={strip_w:.0f}")
        for i, items in enumerate(raw):
            print(f"   线{i}: " + " ".join(
                f"{it[0]:.0f}@{axis}{'x' if axis == 'C' else 'y'}="
                f"{(it[1][0] + it[1][2]) / 2:.0f}"
                for it in sorted(items, key=lambda v: v[0])))
    tasks: List[tuple] = []
    for i, items in enumerate(raw):
        order = 0
        for pos, box, dark, thr, mid, subs, blank in sorted(items, key=lambda it: it[0]):
            if blank:
                continue        # 空行/空列的提示位（游戏画的灰色 0，见 _is_blank_zero）
            # 同一个提示格用「宽 / 窄」两种边距各裁一次，读数都留作候选。
            # 边距还要分方向：沿「数字排列方向」必须留小（提示格挨得很近，
            # 实测上下只差 1px），垂直方向可以留大（留白多，OCR 更准）。
            crops: List[Image.Image] = []
            first_box = None
            for pad_along, pad_perp in ((5, 8), (3, 5)):
                if axis == "R":
                    dx, dy = pad_along, pad_perp
                else:
                    dx, dy = pad_perp, pad_along
                b = (int(box[0]) - dx, int(box[1]) - dy, int(box[2]) + dx, int(box[3]) + dy)
                b = (max(0, b[0]), max(0, b[1]), min(W, b[2]), min(H, b[3]))
                if b[2] - b[0] < 3 or b[3] - b[1] < 3:
                    continue
                if first_box is None:
                    first_box = b
                crops.append(img.crop(b))
            if not crops:
                continue
            # 排障用：MIKU_BOT_OCR_DUMP=<目录> 时把「真正喂给 OCR 的裁框」落盘，
            # 外带一张同样大小的上下文图（各方向多留 40px），用来跟「整格对齐的框」逐像素比。
            if os.environ.get("MIKU_BOT_OCR_DUMP"):
                dmp = Path(os.environ["MIKU_BOT_OCR_DUMP"])
                dmp.mkdir(parents=True, exist_ok=True)
                for k, cr in enumerate(crops):
                    cr.save(dmp / f"{axis}{i:02d}_o{order}_{k}.png")
                img.crop((max(0, int(box[0]) - 40), max(0, int(box[1]) - 40),
                          min(W, int(box[2]) + 40), min(H, int(box[3]) + 40))).save(
                    dmp / f"{axis}{i:02d}_o{order}_ctx.png")
            if boxes is not None and first_box is not None:
                boxes.append(first_box)
            tasks.append(((axis, i, order), crops, dark, thr, mid, subs))
            order += 1

    values: Dict[int, List[List[int]]] = {i: [] for i in range(boss)}
    unreadable: List[tuple] = []
    if not tasks:
        return values, unreadable
    max_value = geom.cols if axis == "R" else geom.rows

    def _ocr_task(item):
        key, crops, dark, thr, mid, subs = item
        cands: List[int] = []
        joined_cands: List[int] = []
        # 「3 / 5」摇摆时拿字形结构定夺（见 _digit_side）：两位数取末位那个子块，
        # 单个字就直接量整块。None = 特征不明显，不插嘴。
        side: Optional[int] = None
        if subs:
            # 块里有 2~3 个墨迹子块 → 是两位数（10~15）。逐个认字再拼起来：
            # 每个子块除 OCR 读数外，还留一个「窄笔画=1」的可能（实测「12」的
            # 那个「1」常被读成 7 之类的，拼出来 72 越界，把整个两位数丢掉）。
            per_slot: List[List[int]] = []
            diag: List[str] = []
            for si, (sx0, sy0, sx1, sy1) in enumerate(subs):
                pad = 1
                sb = (max(0, sx0 - pad), max(0, sy0 - pad), min(W, sx1 + pad), min(H, sy1 + pad))
                opts: List[int] = []
                d = ocr_cell(img.crop(sb), thr, mid)
                if d is not None:
                    if 0 <= d <= 9:
                        opts.append(d)
                    else:
                        # 子块裁到了旁边字符的墨迹（「12」的第 2 块被读成 12）→ 取末位
                        opts.append(d % 10)
                # ★ 单字子块优先用字形库判读（2026-09-30 加）。
                #   钩子原来只查"整个槽位"的裁图，而两位数的槽位在库里**没有对应模板**
                #   （库的类别来自已确认线，两位数样本极少）——于是 10~19 那一片完全没被保护，
                #   这正是 Lv3-016 读不出来（行和 328 ≠ 列和 326）的缺口。
                #   这里改成：**每个单字子块单独查库**，库可信就以库为准，两位数自然由
                #   "库验证过的单个数字"拼出来。
                if GLYPH_LIB_ON:
                    try:
                        gsub = glyph_read(np.asarray(img.crop(sb).convert("L"),
                                                     dtype=np.float32))
                    except Exception:
                        gsub = None
                    if gsub is not None:
                        opts = [gsub]
                pw, ph = sx1 - sx0, sy1 - sy0
                # 「1」的墨迹只有一根竖条（实测约 5px 宽）。2026-09-30 曾把门槛加上下界
                # （4.5px）想把提示区边缘的 3px 装饰竖条排除掉 —— **实测有害**：
                # 读数从 326/324 掉到 276/284，因为更细的子块拿不到候选会让**整个槽位作废**
                # （下面 `if not opts: per_slot = []; break`）。已改回原样，
                # 装饰竖条只能在切块阶段剔（见 _digit_blobs）。
                if (pw <= max(4.0, 0.20 * pitch_hint) and ph >= perp_cell * 0.3
                        and 1 not in opts):
                    opts.append(1)
                if si == len(subs) - 1:
                    side = _digit_side(img.crop(sb), mid)   # 末位（个位）那块的字形结构
                diag.append(f"{pw}x{ph}:{opts}(ocr={d})")
                if not opts:
                    per_slot = []
                    break
                per_slot.append(opts)
            if os.environ.get("MIKU_BOT_TRACE"):
                print(f"[subs] {key} perp_cell={perp_cell:.1f} pitch={pitch_hint:.1f} "
                      + " | ".join(diag))
            if per_slot:
                for combo in itertools.product(*per_slot):
                    j = int("".join(str(d) for d in combo))
                    if 10 <= j <= max_value and j not in joined_cands:
                        joined_cands.append(j)
                joined_cands = joined_cands[:4]
        whole: List[int] = []
        for crop in crops:
            for c in ocr_cell_candidates(crop, thr, mid):
                if c is not None and c not in whole:
                    whole.append(c)
        if len(subs) >= 2:
            # 块里拆得出 2 个墨迹子块 → 这是个两位数（10~15）。
            # 实测整块 OCR 经常把「14」读成「4」、「12」读成「2」（漏读首位），
            # 所以两位数候选（整块读出来的 + 拆字拼接出来的）排在前面，
            # 一位数读数压到最后当备选，由「行列总和必须相等」的约束来定夺。
            two = [v for v in whole if v >= 10]
            for v in joined_cands:
                if v not in two:
                    two.append(v)
            # 游戏里两位数只在 10~15，十位恒为细笔画「1」。若首个墨迹子块是细笔画
            # （=1），十位就已确定为 1：先用整块的一位读数 v 兜一个「1v」候选（保持原
            # 有的首选顺序），再把 10~min(19, max_value) 整体补进来。
            # 实测整块常把「13」读成「5」、拆字又把「3」读成「4」拼出 14，只靠「整块
            # 的一位读数」拼十位会彻底漏掉真值 13——所以必须整体补全。真伪交给
            # 「行和=列和 + 可解」约束定夺。
            glue: set = set()          # 「由真实个位读数补十位」得到的值（算摇摆证据）
            # ⚠ 2026-09-30 试过给这条判断补下界（4.5px，想把 3px 装饰竖条排除掉）——
            #   **实测有害**：读数从 326/324 掉到 276/284 并多出读不出的格子，
            #   因为真实两位数的十位「1」也在这个宽度区间，砍掉就把真值一起砍了。
            #   结论：这里**不能**靠宽度分辨，装饰竖条必须在**切块阶段**就剔掉（见 _digit_blobs）。
            if len(subs) == 2 and (subs[0][2] - subs[0][0]) <= max(4.0, 0.20 * pitch_hint):
                for v in whole:
                    if 0 <= v <= 9 and 10 + v <= max_value:
                        if (10 + v) not in two:
                            two.append(10 + v)
                        glue.add(10 + v)
                for v in range(10, min(19, max_value) + 1):
                    # 兜底的全量清单：只当备选，**不算**「摇摆证据」（它天然同时含 13 和 15）
                    if v not in two:
                        two.append(v)
            one = [v for v in whole if v < 10]
            cands = two + one
            # 字形结构说末位是 3 / 5，而 OCR 恰好在这两个之间摇摆（候选里同时有 …3 和 …5）
            # → 把结构支持的那个排到前面（13 压过 15）。只在「真的摇摆」时才动，
            # 免得把「14」这类读得好好的值改坏。
            # 字形结构（_digit_side）定夺「3 / 5」时，只允许它拿 **OCR 自己读出来的值**当证据：
            # 整块读出的 whole、拆字拼出的 joined_cands。上面为了兜底补进来的「10+个位」和
            # 「10~19 全补一遍」**不算证据**——它们天然同时含 13 和 15，拿它们当证据会把每一个
            # 两位数都误判成「正在 3/5 之间摇摆」，然后结构特征（只在 3/5 上标定过，遇到 4/7
            # 根本不准）把 13 顶到首选。实测真机 Lv3-006：R 轴 6 条两位数整块明明读对了
            # 14/17/1-17/2-14，全被这个误判顶成 13；把证据收窄后它们全部回到正确读数。
            evidence = (set(whole) | set(joined_cands) | glue) & set(two)
            if side is not None:
                other = 5 if side == 3 else 3
                if (any(v % 10 == side for v in evidence)
                        and any(v % 10 == other for v in evidence)):
                    two.sort(key=lambda v: 0 if v % 10 == side else 1)
                    cands = two + one
        else:
            # 单块：整块读出来的结果优先；「拆成两个字再拼」的结果只作补充
            # （实测「13」拆开拼接会被读成 15，而整块读能直接读对）
            side = _digit_side(crops[0], mid)
            cands = list(whole)
            for v in joined_cands:
                if v not in cands:
                    cands.append(v)
            # 同上：单个字在 3 / 5 之间摇摆时，用字形结构定夺
            if side is not None and side in cands and (5 if side == 3 else 3) in cands:
                cands.sort(key=lambda v: 0 if v == side else 1)
        # ★ 字形库判读（独立于 OCR 的判据，2026-09-30 加）
        #   背景：小字形上原始 OCR 会给出垃圾值（20×20 的题里读到 32/62/78），
        #   而纠错机制会从候选里挑一个"看起来合理"的拼成读数——结果自洽、可解、唯一解，
        #   却是**拼出来的**，所有下游检查（总和/可解性/图案比对）都拿它核对自身，抓不到。
        #   规则：库**可信**（相似度≥0.72 且与次选分差≥0.10）→ **只留库的值**；
        #         库**不可信** → 不插手（退化为原来的 OCR 行为，不引入新风险）。
        if GLYPH_LIB_ON and crops:
            cands_ocr = list(cands)
            try:
                gv = glyph_read(np.asarray(crops[0].convert("L"), dtype=np.float32))
            except Exception:
                gv = None
            if gv is not None:
                cands = [gv]
            if os.environ.get("MIKU_BOT_TRACE"):
                print(f"[glyph] {key} 库判={gv} 原候选={cands_ocr}")
        if os.environ.get("MIKU_BOT_TRACE"):
            print(f"[pick] {key} subs={len(subs)} whole={whole} "
                  f"joined={joined_cands} -> {cands}")
        return key, cands, dark, thr, len(subs)

    tmp: Dict[tuple, List[int]] = {}
    n_workers = max(1, min(int(workers), max(2, len(tasks))))
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        for key, val, dark, thr, n_subs in ex.map(_ocr_task, tasks):
            tmp[key] = val
            if os.environ.get("MIKU_BOT_TRACE"):
                print(f"[trace {axis}] {key} -> {val} (dark={dark}, thr={thr:.0f}, subs={n_subs})")
            if not val and dark >= 15:
                unreadable.append(key)
    for i in range(boss):
        order = 0
        while (axis, i, order) in tmp:
            val = tmp[(axis, i, order)]
            if val:
                values[i].append(val)
            order += 1
    return values, unreadable


def _read_frame(screen: Screen, geom: Geometry, cfg: Config
                ) -> Tuple[List[List[List[int]]], List[List[List[int]]], List[tuple]]:
    """读取一帧：返回（行候选、列候选、无法识别的块）。每个提示是一个候选值列表。"""
    img = screen.grab_client()
    workers = max(2, int(cfg.ocr_workers))
    rows_map, bad_r = _read_clue_axis(img, geom, "R", workers)
    cols_map, bad_c = _read_clue_axis(img, geom, "C", workers)
    rows = [rows_map.get(r, []) for r in range(geom.rows)]
    cols = [cols_map.get(c, []) for c in range(geom.cols)]
    return rows, cols, list(bad_r) + list(bad_c)


def _primary(axis_cands: List[List[List[int]]]) -> List[List[int]]:
    """取每条线的首选读数。"""
    return [[c[0] for c in line] for line in axis_cands]


def validate_clues(rows: List[List[int]], cols: List[List[int]], geom: Geometry) -> List[str]:
    problems: List[str] = []
    if len(rows) != geom.rows or len(cols) != geom.cols:
        problems.append("行列数与校准数据不一致")
        return problems
    for r, clue in enumerate(rows):
        if len(clue) > geom.max_row_slots:
            problems.append(f"第{r + 1}行提示过多")
        for v in clue:
            if v < 1 or v > geom.cols:
                problems.append(f"第{r + 1}行出现越界提示 {v}")
        if sum(clue) > geom.cols:
            problems.append(f"第{r + 1}行提示总和（{sum(clue)}）超过行长度")
    for c, clue in enumerate(cols):
        if len(clue) > geom.max_col_slots:
            problems.append(f"第{c + 1}列提示过多")
        for v in clue:
            if v < 1 or v > geom.rows:
                problems.append(f"第{c + 1}列出现越界提示 {v}")
        if sum(clue) > geom.rows:
            problems.append(f"第{c + 1}列提示总和（{sum(clue)}）超过列长度")
    rs, cs = sum(map(sum, rows)), sum(map(sum, cols))
    if rs != cs:
        problems.append(f"行提示总和({rs}) ≠ 列提示总和({cs})，识别有误")
    if rs == 0:
        problems.append("没有识别到任何提示数字")
    return problems


def _clue_alts(axis_cands: List[List[List[int]]], is_row: bool) -> List[tuple]:
    """收集所有「备选读数」：((is_row, line, idx), value)。"""
    out = []
    for li, line in enumerate(axis_cands):
        for idx, cands in enumerate(line):
            for v in cands[1:]:
                out.append(((is_row, li, idx), v))
    return out


def _apply_alt(rows: List[List[int]], cols: List[List[int]], key: tuple, val: int):
    is_row, li, idx = key
    target = rows if is_row else cols
    old = target[li][idx]
    target[li][idx] = val
    return old


def fix_by_sum_constraint(rows_c: List[List[List[int]]], cols_c: List[List[List[int]]],
                          geom: Geometry, cfg: Config, log=print
                          ) -> Optional[Tuple[List[List[int]], List[List[int]]]]:
    """
    用「行提示总和必须等于列提示总和」这一硬约束，配合每个提示格的候选读数
    自动纠错：先试改一个提示，再试改两个；每个候选解都用求解器验证可解。

    **2026-09-29 17:5x：加了零成本的总和预筛（这是关键修复）。** 原来「改两处」是
    每一对都去调一次求解器，只靠 2 秒时间预算兜底 —— 20×20 上备选两万多个，实测**根本搜不完**，
    于是「本来能纠回来的读数」被当成识别失败（真机 Lv3 上反复出现）。现在先用
    「改完行和必须还等于列和」把组合筛掉（纯加减法，不花时间），只有过筛的才调求解器：
    实测 25345 对 → **0.2 秒**筛完（过筛 1295 对）→「改一两处」第一次是**搜完整**的。
    过筛的对按「改动量小的优先」排序，并仍留一个时间预算兜底。
    """
    rows, cols = _primary(rows_c), _primary(cols_c)
    alts = _clue_alts(rows_c, True) + _clue_alts(cols_c, False)
    if not alts:
        return None
    s_row, s_col = sum(map(sum, rows)), sum(map(sum, cols))

    # 每个备选对「行和 / 列和」的影响（改前的值就是主读数，所以 delta = 新值 - 主读数）
    cand: List[Tuple[tuple, int, int, int]] = []
    for key, val in alts:
        is_row, li, idx = key
        old = (rows if is_row else cols)[li][idx]
        d = val - old
        cand.append((key, val, d if is_row else 0, 0 if is_row else d))

    def _ok(r, c) -> bool:
        if validate_clues(r, c, geom):
            return False
        return PuzzleSolver(r, c).solve(min(6.0, cfg.solver_time_limit)) is not None

    # 一处：只有能把差额补平的备选才值得调求解器（总和相等是必要条件）
    for key, val, dr, dc in cand:
        if s_row + dr != s_col + dc:
            continue
        old = _apply_alt(rows, cols, key, val)
        if _ok(rows, cols):
            log(f"  已按行列总和约束修正一处读数：{key} 改为 {val}（原 {old}）")
            return rows, cols
        _apply_alt(rows, cols, key, old)
    # 两处：同样先过总和筛（这才是搜得完的关键），再按「改动量小的优先」试。
    # 总和本来就相等时也要试：某个数字读错后总和恰好还相等、但整题无解的那种。
    pairs: List[Tuple[int, int, int]] = []
    for i in range(len(cand)):
        k1, _v1, d1r, d1c = cand[i]
        for j in range(i + 1, len(cand)):
            k2, _v2, d2r, d2c = cand[j]
            if k1 == k2:                    # 同一个提示格的两种备选，不能同时改
                continue
            if s_row + d1r + d2r != s_col + d1c + d2c:
                continue                    # 零成本预筛：改完总和还是不等，直接丢
            pairs.append((abs(d1r) + abs(d1c) + abs(d2r) + abs(d2c), i, j))
    pairs.sort()
    budget = time.time() + max(6.0, cfg.solver_time_limit)
    tried = 0
    for _w, i, j in pairs:
        if time.time() > budget:
            log(f"  两处纠错：过总和筛共 {len(pairs)} 对，预算内试了 {tried} 对，先放弃")
            break
        tried += 1
        k1, v1, _d1r, _d1c = cand[i]
        k2, v2, _d2r, _d2c = cand[j]
        o1 = _apply_alt(rows, cols, k1, v1)
        o2 = _apply_alt(rows, cols, k2, v2)
        if _ok(rows, cols):
            log(f"  已按行列总和约束修正两处读数：{k1}→{v1}, {k2}→{v2}")
            return rows, cols
        _apply_alt(rows, cols, k2, o2)
        _apply_alt(rows, cols, k1, o1)
    if pairs:
        log(f"  两处纠错试完 {tried}/{len(pairs)} 对（都过了总和筛），没有可解的组合")
    return None


def read_puzzle(screen: Screen, geom: Geometry, cfg: Config, log=print) -> Puzzle:
    park_cursor(screen)
    good: List[Puzzle] = []
    last_problems: List[str] = []
    attempts = max(1, int(cfg.max_read_attempts))
    for attempt in range(1, attempts + 1):
        # 先等画面稳定（进关动画/高亮闪烁停下来）再 OCR，别抢在动画中间帧读，
        # 否则会读出「第7列 37」这种半截数字，平白多跑几帧
        _wait_board_stable(screen, geom)
        rows_c, cols_c, unreadable = _read_frame(screen, geom, cfg)
        rows, cols = _primary(rows_c), _primary(cols_c)
        problems = validate_clues(rows, cols, geom)
        if problems:
            fixed = fix_by_sum_constraint(rows_c, cols_c, geom, cfg, log)
            if fixed is not None:
                rows, cols = fixed
                problems = []
        elif unreadable:
            # 读数本身已经完全通过校验（无越界、行和=列和），只是提示条带里多出几团
            # 读不出来的墨迹——多半是光标高亮边框、界面美术混进了条带。它不影响结果，
            # 忽略即可，别因为它把整帧判死（这就是「1 个提示格识别失败」的成因）。
            unreadable = []
        if unreadable:
            problems = [f"{len(unreadable)} 个提示格识别失败"] + problems
        if not problems:
            # 提示过了「行列总和相等」也可能仍然读错（错一个数字后总和恰好还相等）；
            # 用求解器确认一下，解不出来就按候选读数纠错
            try:
                solvable = PuzzleSolver(rows, cols).solve(
                    min(6.0, cfg.solver_time_limit)) is not None
            except Exception:
                solvable = False
            if not solvable:
                fixed = fix_by_sum_constraint(rows_c, cols_c, geom, cfg, log)
                if fixed is not None:
                    rows, cols = fixed
                else:
                    problems = ["提示数字看似自洽但无解，识别有误"]
        if problems:
            last_problems = problems
            log(f"  第 {attempt}/{attempts} 帧校验未通过：{'；'.join(problems[:3])}")
            time.sleep(0.15)
            continue
        puzzle = Puzzle(rows, cols, geom.rows, geom.cols)
        if good and good[-1].key() == puzzle.key():
            return puzzle
        good.append(puzzle)
        log(f"  第 {attempt}/{attempts} 帧识别通过（行和={sum(puzzle.row_sums)}），等待第二帧确认…")
    if good:
        cnt = Counter(p.key() for p in good)
        times = cnt.most_common(1)[0][1]
        if times >= 2:
            return good[-1]
        log("  警告：多帧结果不一致，暂用最后一帧（建议先 --dry-run 核对）")
        return good[-1]
    # ★ 读不出来时也留现场（2026-09-30 加）。
    #   之前只有「涂到一半被重置」那条路存了帧，读失败这条没存 ——
    #   结果 Lv3-016 / Lv3-064 一直读不出来，却拿不到它们的原始画面离线修。
    try:
        if not getattr(screen, "is_fake", False):
            GLYPH_LIB_DIR.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            screen.grab_client().save(GLYPH_LIB_DIR / f"{stamp}_READFAIL_frame.png")
            (GLYPH_LIB_DIR / f"{stamp}_READFAIL_data.json").write_text(json.dumps({
                "problems": list(last_problems),
                "rows": [[int(v) for v in ln] for ln in rows],
                "cols": [[int(v) for v in ln] for ln in cols],
                "geometry": {"rows": geom.rows, "cols": geom.cols,
                             "cell_w": geom.cell_w, "x1": geom.x1, "y1": geom.y1},
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            log(f"  [存档] 读不出来的现场已存到 debug/glyph_verified/{stamp}_READFAIL_*")
    except Exception as exc:
        log(f"  [存档] 读失败现场存盘失败（不影响流程）：{exc}")
    raise BotAbort("识别失败：" + ("；".join(last_problems[:3]) if last_problems else "多帧均无法通过校验"))


# --------------------------------------------------------------------------
# 求解器
# --------------------------------------------------------------------------


@lru_cache(maxsize=8192)
def _line_analyze(length: int, clues: Tuple[int, ...], must_fill: int, must_empty: int):
    """
    精确分析单行/列的动态规划（不枚举全部摆法）：

      can_fill  —— 在某些合法摆法里会被填的格子
      can_empty —— 在某些合法摆法里会留空的格子
      count     —— 合法摆法总数

    必然填 = ~can_empty，必然空 = ~can_fill；两种都不属于某格 = 矛盾。
    返回 None 表示矛盾。
    """
    k = len(clues)
    full = (1 << length) - 1
    fwd = [[False] * (k + 1) for _ in range(length + 2)]
    cnt = [[0] * (k + 1) for _ in range(length + 2)]
    fwd[0][0] = 1 != 0
    cnt[0][0] = 1
    for i in range(length + 1):
        for j in range(k + 1):
            if not fwd[i][j]:
                continue
            if i < length and not (must_fill >> i) & 1:      # 该格留空
                fwd[i + 1][j] = True
                cnt[i + 1][j] += cnt[i][j]
            if j < k:                                        # 该格开始放第 j 段
                b = clues[j]
                if i + b <= length:
                    seg = ((1 << b) - 1) << i
                    if not (seg & must_empty):
                        ni = i + b
                        if ni >= length or not (must_fill >> ni) & 1:
                            tgt = ni + 1 if ni < length else length
                            fwd[tgt][j + 1] = True
                            cnt[tgt][j + 1] += cnt[i][j]
    if not fwd[length][k]:
        return None
    bk = [[False] * (k + 1) for _ in range(length + 2)]
    bk[length][k] = True
    for i in range(length, -1, -1):
        for j in range(k, -1, -1):
            if not bk[i][j]:
                continue
            if i - 1 >= 0 and not (must_fill >> (i - 1)) & 1:
                bk[i - 1][j] = True
            if j - 1 >= 0:
                b = clues[j - 1]
                starts = [i - b - 1] if i < length else [length - b - 1, length - b]
                for i0 in starts:
                    if i0 < 0 or i0 + b > length:
                        continue
                    seg = ((1 << b) - 1) << i0
                    if seg & must_empty:
                        continue
                    ni = i0 + b
                    if ni < length and (must_fill >> ni) & 1:
                        continue
                    bk[i0][j - 1] = True
    can_fill = can_empty = 0
    for i in range(length):
        bit = 1 << i
        for j in range(k + 1):
            if not fwd[i][j]:
                continue
            if not (must_fill & bit) and bk[i + 1][j]:
                can_empty |= bit
            if j < k:
                b = clues[j]
                if i + b <= length:
                    seg = ((1 << b) - 1) << i
                    if not (seg & must_empty):
                        ni = i + b
                        if ni >= length or not (must_fill >> ni) & 1:
                            tgt = ni + 1 if ni < length else length
                            if bk[tgt][j + 1]:
                                can_fill |= seg
                                if ni < length:
                                    can_empty |= 1 << ni   # 块与块之间的分隔格必空
    if (can_fill | can_empty) & full != full:
        return None
    return can_fill, can_empty, cnt[length][k]


@lru_cache(maxsize=4096)
def _first_layouts(length: int, clues: Tuple[int, ...], must_fill: int, must_empty: int,
                   limit: int) -> Tuple[int, ...]:
    """只取前 limit 个合法摆法（分支搜索用）。"""
    out: List[int] = []
    k = len(clues)
    suffix = [0] * (k + 1)
    for i in range(k - 1, -1, -1):
        suffix[i] = suffix[i + 1] + clues[i] + (1 if i + 1 < k else 0)

    def rec(i: int, j: int, mask: int) -> None:
        if len(out) >= limit:
            return
        if j == k:
            rest = ((1 << (length - i)) - 1) << i
            if not (rest & must_fill):
                out.append(mask)
            return
        b = clues[j]
        for start in range(i, length - suffix[j] + 1):
            if len(out) >= limit:
                return
            seg = ((1 << b) - 1) << start
            if seg & must_empty:
                continue
            gap = ((1 << (start - i)) - 1) << i
            if gap & must_fill:
                continue
            ni = start + b
            if ni < length and (must_fill >> ni) & 1:
                continue
            rec(ni + 1 if ni < length else length, j + 1, mask | seg)

    rec(0, 0, 0)
    return tuple(out)


def clues_of_line(cells: Sequence[int]) -> List[int]:
    out: List[int] = []
    run = 0
    for v in cells:
        if v == 1:
            run += 1
        elif run:
            out.append(run)
            run = 0
    if run:
        out.append(run)
    return out


class PuzzleSolver:
    """0=未知, 1=填, -1=空。约束传播为主，必要时分支搜索兜底。"""

    def __init__(self, row_clues: Sequence[Sequence[int]], col_clues: Sequence[Sequence[int]]):
        self.rows_n = len(row_clues)
        self.cols_n = len(col_clues)
        self.row_clues = [tuple(c) for c in row_clues]
        self.col_clues = [tuple(c) for c in col_clues]
        self.grid = [[0] * self.cols_n for _ in range(self.rows_n)]

    # -- 基础工具 ---------------------------------------------------------
    def _state_masks(self, axis: str, idx: int) -> Tuple[int, int]:
        filled = empty = 0
        if axis == "R":
            for c in range(self.cols_n):
                v = self.grid[idx][c]
                if v == 1:
                    filled |= 1 << c
                elif v == -1:
                    empty |= 1 << c
        else:
            for r in range(self.rows_n):
                v = self.grid[r][idx]
                if v == 1:
                    filled |= 1 << r
                elif v == -1:
                    empty |= 1 << r
        return filled, empty

    def _analyze(self, axis: str, idx: int):
        filled, empty = self._state_masks(axis, idx)
        clues = self.row_clues[idx] if axis == "R" else self.col_clues[idx]
        length = self.cols_n if axis == "R" else self.rows_n
        return _line_analyze(length, clues, filled, empty)

    def _refine(self, axis: str, idx: int) -> Optional[List[Tuple[str, int]]]:
        """把该行/列中确定的格子写回棋盘，返回受影响的交叉线列表。"""
        length = self.cols_n if axis == "R" else self.rows_n
        full = (1 << length) - 1
        filled, empty = self._state_masks(axis, idx)
        res = self._analyze(axis, idx)
        if res is None:
            return None
        can_fill, can_empty, _ = res
        new_f = (~can_empty & full) & ~filled
        new_e = (~can_fill & full) & ~empty
        if not new_f and not new_e:
            return []
        touched: List[Tuple[str, int]] = []
        for mask, val in ((new_f, 1), (new_e, -1)):
            m = mask
            while m:
                b = m & -m
                i = b.bit_length() - 1
                m ^= b
                if axis == "R":
                    self.grid[idx][i] = val
                    touched.append(("C", i))
                else:
                    self.grid[i][idx] = val
                    touched.append(("R", i))
        return touched

    def _propagate(self) -> bool:
        queue = deque([("R", r) for r in range(self.rows_n)] + [("C", c) for c in range(self.cols_n)])
        inq = set(queue)
        while queue:
            axis, idx = queue.popleft()
            inq.discard((axis, idx))
            touched = self._refine(axis, idx)
            if touched is None:
                return False
            for item in touched:
                if item not in inq:
                    inq.add(item)
                    queue.append(item)
        return True

    def _complete(self) -> bool:
        return all(v != 0 for row in self.grid for v in row)

    def _valid(self) -> bool:
        for r in range(self.rows_n):
            if clues_of_line(self.grid[r]) != list(self.row_clues[r]):
                return False
        for c in range(self.cols_n):
            col = [self.grid[r][c] for r in range(self.rows_n)]
            if clues_of_line(col) != list(self.col_clues[c]):
                return False
        return True

    # -- 搜索 -------------------------------------------------------------
    def _search(self, deadline: float) -> bool:
        if time.time() > deadline:
            raise SolverTimeout()
        if not self._propagate():
            return False
        if self._complete():
            return True
        best = None
        for axis in ("R", "C"):
            n = self.rows_n if axis == "R" else self.cols_n
            for idx in range(n):
                res = self._analyze(axis, idx)
                if res is None:
                    return False
                count = res[2]
                if count > 1 and (best is None or count < best[2]):
                    best = (axis, idx, count)
        if best is None:
            return self._complete()
        axis, idx, _ = best
        length = self.cols_n if axis == "R" else self.rows_n
        filled, empty = self._state_masks(axis, idx)
        clues = self.row_clues[idx] if axis == "R" else self.col_clues[idx]
        layouts = _first_layouts(length, clues, filled, empty, 64)
        snapshot = [row[:] for row in self.grid]
        for p in layouts:
            self.grid = [row[:] for row in snapshot]
            for i in range(length):
                val = 1 if (p >> i) & 1 else -1
                if axis == "R":
                    self.grid[idx][i] = val
                else:
                    self.grid[i][idx] = val
            if self._search(deadline):
                return True
        self.grid = [row[:] for row in snapshot]
        return False

    def solve(self, time_limit: float = 20.0) -> Optional[List[List[int]]]:
        if not self._propagate():
            return None
        if not self._complete():
            try:
                if not self._search(time.time() + time_limit):
                    return None
            except SolverTimeout:
                return None
        return self.grid if self._valid() else None


def format_solution(grid: Sequence[Sequence[int]], row_clues: Sequence[Sequence[int]]) -> str:
    if not grid:
        return ""
    width = max((len(" ".join(str(v) for v in c)) for c in row_clues), default=2)
    lines = []
    for r, row in enumerate(grid):
        left = " ".join(str(v) for v in row_clues[r]).rjust(width)
        art = "".join("[]" if v == 1 else "  " for v in row)
        lines.append(f"{left} |{art}|")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 填格 + 回读校验
# --------------------------------------------------------------------------


def fill_cells(grid: Sequence[Sequence[int]]) -> List[Tuple[int, int]]:
    """蛇形顺序，减少鼠标来回移动。"""
    cells: List[Tuple[int, int]] = []
    for r, row in enumerate(grid):
        idxs = range(len(row)) if r % 2 == 0 else range(len(row) - 1, -1, -1)
        cells.extend((r, c) for c in idxs if row[c] == 1)
    return cells


def empty_cells(grid: Sequence[Sequence[int]]) -> List[Tuple[int, int]]:
    """所有该留空的格子（答案里为 0），同样蛇形顺序。"""
    cells: List[Tuple[int, int]] = []
    for r, row in enumerate(grid):
        idxs = range(len(row)) if r % 2 == 0 else range(len(row) - 1, -1, -1)
        cells.extend((r, c) for c in idxs if row[c] == 0)
    return cells


def _serpentine(cells: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """把一组格子按蛇形（奇数行反向）排序，少让鼠标来回跑。"""
    return sorted(cells, key=lambda rc: (rc[0], rc[1] if rc[0] % 2 == 0 else -rc[1]))


def clue_waves(rows: Sequence[Sequence[int]], cols: Sequence[Sequence[int]],
               grid: Sequence[Sequence[int]]) -> List[List[Tuple[int, int]]]:
    """
    把「该填的格子」按**定这一格要多深的推理**分成三波（从易到难）。

    波 1：只看这一行/这一列自己的提示就能定死的格子（空棋盘上的强制位）——
          比如「15」这种占满整行的、或者提示把整行都锁死了的。
    波 2：要靠多轮行列交叉推理才定得出来的格子（约束传播跑完就能拿到）。
    波 3：传播定不出来、只能用分支搜索试探才定下来的格子（最可疑——
          读题错一个数字，这一批最容易跟着错）。

    用途：先涂简单的（1、2 波），让游戏把已满足的行/列提示逐个变灰；
    拿这个当判官自查（judge_clue_dims）通过之后，才去涂第 3 波那批"猜出来的"格子。
    每波内部仍按蛇形排序。
    """
    rows_n = len(grid)
    cols_n = len(grid[0]) if rows_n else 0
    if not rows_n or not cols_n:
        return [[], [], []]
    easy: Set[Tuple[int, int]] = set()
    for r in range(rows_n):
        res = _line_analyze(cols_n, tuple(rows[r]), 0, 0)
        if res is None:
            continue
        can_fill, can_empty, _ = res
        m = can_fill & ~can_empty & ((1 << cols_n) - 1)
        while m:
            b = m & -m
            easy.add((r, b.bit_length() - 1))
            m ^= b
    for c in range(cols_n):
        res = _line_analyze(rows_n, tuple(cols[c]), 0, 0)
        if res is None:
            continue
        can_fill, can_empty, _ = res
        m = can_fill & ~can_empty & ((1 << rows_n) - 1)
        while m:
            b = m & -m
            easy.add((b.bit_length() - 1, c))
            m ^= b
    easy_set = set(easy)
    # 波 2：约束传播到收敛时能定的格子（不含波 1）
    mid: Set[Tuple[int, int]] = set()
    try:
        solver = PuzzleSolver(rows, cols)
        if solver._propagate():
            mid = {(r, c) for r in range(rows_n) for c in range(cols_n)
                   if solver.grid[r][c] == 1 and (r, c) not in easy_set}
    except Exception:
        mid = set()
    tail = [rc for rc in fill_cells(grid) if rc not in easy_set and rc not in mid]
    return [_serpentine(easy), _serpentine(mid), tail]


def fill_order_plan(rows: Sequence[Sequence[int]], cols: Sequence[Sequence[int]],
                    grid: Sequence[Sequence[int]]) -> List[Tuple[str, List[Tuple[int, int]]]]:
    """
    填色顺序：**前两波合起来按蛇形（从上到下、从左到右）铺满**，
    只把第 3 波「只能靠搜索试探才定下来的格子」留到最后涂。

    为什么这么排（2026-09-29 用 Lv2-135 的 164 格真机题实测，判据就是 `judge_clue_dims`
    认的「整条线的格子都涂满 → 游戏必须已把这条线的提示变灰」）：

        分三波（眼/交叉/试探）：每批涂完可验的线数 = [0, 1, 3, 5, 11, 15, 15, 30]
        蛇形铺满：              每批涂完可验的线数 = [1, 3, 5, 7, 11, 17, 30]

    「交叉推理才定得下来的那批格子」常常正是补全开头那几行的最后几块——把它们单独拆成
    一批推到后面，开头几行就一直黑着，判官没东西可看（第一批甚至 0 条可验：
    那条自查纯属白跑）。前两批合并成蛇形后，前 3 批累计可验线数从 4 条涨到 9 条，
    鼠标行程也从 304 格降到 220 格（少跨波、少切碎批）。

    要保住的好处只有一条：**试探出来的格子最后涂**（它对读题错误最敏感）。
    """
    waves = clue_waves(rows, cols, grid)
    tail = list(waves[2])
    tail_set = set(tail)
    body = [rc for rc in fill_cells(grid) if rc not in tail_set]
    plan: List[Tuple[str, List[Tuple[int, int]]]] = []
    if body:
        plan.append(("蛇形铺满", body))
    if tail:
        plan.append(("试探出来的（留到最后）", tail))
    return plan


def _cell_diff(before: Image.Image, after: Image.Image, cx: float, cy: float,
                half: int = 3) -> float:
    box = (int(cx - half), int(cy - half), int(cx + half + 1), int(cy + half + 1))
    box = (max(0, box[0]), max(0, box[1]),
           min(before.width, box[2]), min(before.height, box[3]))
    if box[2] - box[0] < 2 or box[3] - box[1] < 2:
        return 0.0
    a = np.asarray(before.crop(box), dtype=np.float32)
    b = np.asarray(after.crop(box), dtype=np.float32)
    if a.shape != b.shape:
        return 0.0
    return float(np.abs(a - b).mean())


def execute_fill(screen: Screen, geom: Geometry, cells: Sequence[Tuple[int, int]],
                 cfg: Config, stop: StopController, log=print) -> int:
    t0 = time.time()
    global _PLANNED_CLICKS, _PLANNED_SET
    _PLANNED_CLICKS = True          # 计划内的填色：审计闸门放行
    _PLANNED_SET = {(r, c) for r, c in cells}   # 本批该点的格子；落点跑出这个集合就算点歪
    try:
        for i, (r, c) in enumerate(cells, 1):
            stop.check()
            cx, cy = geom.cell_center(r, c)
            screen.click_client(cx, cy, cfg.click_hold_ms / 1000.0)
            if cfg.click_interval_ms > 0:
                time.sleep(cfg.click_interval_ms / 1000.0)
            if cfg.fills_per_pause and i % cfg.fills_per_pause == 0:
                stop.sleep(cfg.pause_ms / 1000.0)
    finally:
        _PLANNED_CLICKS = False
        _PLANNED_SET = set()
    return time.time() - t0


def execute_marks(screen: Screen, geom: Geometry, cells: Sequence[Tuple[int, int]],
                  cfg: Config, stop: StopController, log=print) -> int:
    """右键把「该留空」的格子打叉（纯标注，不影响过关判定）。"""
    t0 = time.time()
    global _PLANNED_CLICKS, _PLANNED_SET
    _PLANNED_CLICKS = True          # 计划内的打叉：审计闸门放行
    _PLANNED_SET = {(r, c) for r, c in cells}
    try:
        for i, (r, c) in enumerate(cells, 1):
            stop.check()
            cx, cy = geom.cell_center(r, c)
            screen.click_client(cx, cy, cfg.click_hold_ms / 1000.0, right=True)
            if cfg.click_interval_ms > 0:
                time.sleep(cfg.click_interval_ms / 1000.0)
            if cfg.fills_per_pause and i % cfg.fills_per_pause == 0:
                stop.sleep(cfg.pause_ms / 1000.0)
    finally:
        _PLANNED_CLICKS = False
    return time.time() - t0


CLUE_DIM_LEVEL = 108      # 提示数字「还是黑的」的上界：实测黑字最暗 60~83、满足后的灰字 122~180


def clue_line_ink(img: Image.Image, geom: Geometry, axis: str, i: int) -> Optional[float]:
    """
    一条提示线里最深的墨迹亮度（黑字 ≈64、游戏满足之后染的灰字 ≈165~180），没墨迹返回 None。

    条带取「提示面板」那一段：靠棋盘一侧是数字，往外是面板留白。面板外面可能是深色的
    界面（顶部信息栏、立绘、计时框），照进来会把「最暗的像素」全带成黑色，判官就全瞎了。
    所以从外往内切：只要最外那一列/行的中位亮度是暗的（不是面板底色），就说明还没进面板。
    """
    if axis == "R":
        border = geom.x1 - geom.cell_w / 2.0
        x0 = max(0, int(border - geom.max_row_slots * geom.cell_w))
        x1 = min(img.width, max(1, int(border - 3)))
        c, half = geom.y1 + i * geom.cell_h, geom.cell_h * 0.45
        box = (x0, max(0, int(c - half)), x1, min(img.height, int(c + half) + 1))
    else:
        border = geom.y1 - geom.cell_h / 2.0
        y0 = max(0, int(border - geom.max_col_slots * geom.cell_h))
        y1 = min(img.height, max(1, int(border - 3)))
        c, half = geom.x1 + i * geom.cell_w, geom.cell_w * 0.45
        box = (max(0, int(c - half)), y0, min(img.width, int(c + half) + 1), y1)
    if box[2] - box[0] < 6 or box[3] - box[1] < 6:
        return None
    g = np.asarray(img.convert("L").crop(box)).astype(np.float32)
    if axis == "C":
        g = g.T                      # 转置成「沿方向 = 第一维」，两个轴一套代码
    while g.shape[1] > 6 and float(np.median(g[:, 0])) < 200.0:
        g = g[:, 1:]                 # 最外那一列是暗的 → 面板还没开始
    if g.size < 24 or g.shape[1] < 4:
        return None
    # 取「第 k 小」的像素值（k 是个很小的固定数），不用 2% 分位：
    # 只有一两个数字的提示线，墨迹面积可能不到条带的 1%，2% 分位会落在面板留白上，
    # 被误判成「已经变灰」。取前若干个最暗像素里偏后的那个，既躲开个别噪点，
    # 又一定落在数字笔画里。
    vals = np.sort(g.ravel())
    idx = min(vals.size - 1, max(2, int(0.002 * vals.size)))
    return float(vals[idx])


def clue_line_highlighted(img: Image.Image, geom: Geometry, axis: str, i: int) -> bool:
    """这条线的提示数字现在是不是被游戏染成粉色了（= 光标正停在这一行/这一列）。

    2026-09-30 凌晨实测：游戏会把光标所在行/列的提示高亮成粉色，**被高亮的「已满足」提示
    墨迹亮度从 162~180 掉到 122**，离"变灰"阈值（108）只差 14——这个测量值不可靠，
    拿它判"该变灰却没变灰"就会误报（真机 Lv3-028 就是这么把快涂完的关卡重置掉的）。
    所以：高亮时**跳过这一条线**，别拿被污染的像素去定罪。
    """
    x0 = y0 = x1 = y1 = 0
    if axis == "R":
        x0, x1 = int(geom.x1 - 3.2 * geom.cell_w), int(geom.x1 - 0.1 * geom.cell_w)
        y0, y1 = int(geom.y1 + i * geom.cell_h), int(geom.y1 + (i + 1) * geom.cell_h)
    else:
        x0, x1 = int(geom.x1 + i * geom.cell_w), int(geom.x1 + (i + 1) * geom.cell_w)
        y0, y1 = int(geom.y1 - 3.2 * geom.cell_h), int(geom.y1 - 0.1 * geom.cell_h)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.width, x1), min(img.height, y1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return False
    a = np.asarray(img.crop((x0, y0, x1, y1)).convert("RGB"), dtype=np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    pink = (r > 200) & (g > 90) & (g < 190) & (b > 120) & (b < 210)
    return bool(pink.mean() > 0.05)


def audit_click_targeting(img: Image.Image, geom: Geometry,
                          rows: Sequence[Sequence[int]], cols: Sequence[Sequence[int]],
                          grid: Sequence[Sequence[int]],
                          painted: Iterable[Tuple[int, int]], log=print) -> List[str]:
    """反查「点击落点」：找出**账本说没涂完、游戏却已经变灰**的线。

    这是判"是读题错还是点歪了"的分水岭，2026-09-30 凌晨从 Lv3-028 现场存档里挖出来的：

      · 读题错（提示数字读错）→ 图案对不上 → 那条线**永不变灰**；
        但**绝不会**反过来出现"账本明明还没涂、游戏却变灰了"。
      · 点歪（这一下落到别的格）→ 目标格空着、**别的格被涂** →
        那行/列的图案可能**碰巧凑齐**而变灰 —— 也就是"反着"的矛盾。
        反过来，被涂错的那条线又永远变灰不了（多了一格，擦不掉）。

    所以只要出现一条反向矛盾，就是**点击落点问题**，而不是读题问题；
    对这两种情况的处理完全不同（前者该换读数，后者该重置本关重来 + 记录落点）。
    """
    painted_set = set(painted)
    out: List[str] = []
    for axis, seq, n in (("R", rows, geom.rows), ("C", cols, geom.cols)):
        for i in range(n):
            cells = [(i, c) for c in range(geom.cols) if grid[i][c] == 1] if axis == "R" \
                else [(r, i) for r in range(geom.rows) if grid[r][i] == 1]
            if all(cell in painted_set for cell in cells):
                continue                     # 账本说涂完了，不在本函数关心范围
            ink = clue_line_ink(img, geom, axis, i)
            if ink is not None and ink >= CLUE_DIM_LEVEL:
                out.append(f"{'行' if axis == 'R' else '列'}{i + 1}")
    if out:
        log(f"  ⚠ 点击落点反查：{len(out)} 条线「账本说没涂完、游戏却已变灰」"
            f"（{out[:8]}）—— 这是**点歪**的铁证，不是读题错：有格子被涂到了计划外的地方。")
    return out


def judge_clue_dims(img: Image.Image, geom: Geometry,
                    rows: Sequence[Sequence[int]], cols: Sequence[Sequence[int]],
                    grid: Sequence[Sequence[int]], painted: Iterable[Tuple[int, int]],
                    baseline: Optional[Image.Image] = None
                    ) -> List[str]:
    """
    拿**游戏自己的状态**当判官，核对这次读题/求解对不对。

    规则只有一条：只要「按我的解，这一行/列该填的格子已经全涂上了」（提示是 0 的空行/空列
    天然满足），游戏就必须已经把它的提示数字**变灰**；还黑着就说明这条线的读题有误
    ——或者格子涂在了别处。比再 OCR 读一遍提示便宜得多（纯像素，不用 Tesseract），
    而且不会受 OCR 误读影响（实测真机：游戏把满足的行/列提示逐个染灰，灰 ~165 vs 黑 ~64）。

    返回对不上的线名（如 ["行3", "列7"]），空列表 = 全部对得上。
    """
    painted_set = set(painted)
    bad: List[str] = []
    highlighted: List[str] = []               # 被光标高亮、这帧测不准的线（跳过不判）
    blanks: List[Tuple[str, float]] = []      # 我读成「这行/列不用填」，游戏那边还黑着的
    for axis, n in (("R", geom.rows), ("C", geom.cols)):
        for i in range(n):
            if axis == "R":
                cells = [(i, c) for c in range(geom.cols) if grid[i][c] == 1]
            else:
                cells = [(r, i) for r in range(geom.rows) if grid[r][i] == 1]
            ink = clue_line_ink(img, geom, axis, i)
            if ink is None:
                continue
            label = f"{'行' if axis == 'R' else '列'}{i + 1}"
            # 注意：**不要**在这里按"粉色高亮"跳过整条线（2026-09-30 凌晨改）。
            # 游戏的行/列提示底色是**三色交替**的（浅绿/白/粉红），粉底本身就会把"已变灰"的
            # 提示亮度从 162~180 压到 ~122——按高亮跳过会把 1/3 的线永远排除在自查之外。
            # 真正稳妥的办法是用**这条线自己的黑色基准**来比（见下面 baseline 参数）。
            if not cells:
                blanks.append((label, ink))
                continue
            if not all(cell in painted_set for cell in cells):
                continue                      # 这条线还没涂完，不作判断
            # 判定"变灰了没"：优先用**这条线自己的黑色基准**（进关那帧的同一位置），
            # 因为提示底色是三色交替的，粉底线天然就比白底线暗 40 左右（实测 180→122）。
            # 拿自己的基准比，底色差异自动抵消；只有基准拿不到时才退回全局阈值。
            thr = CLUE_DIM_LEVEL
            if baseline is not None:
                base_ink = clue_line_ink(baseline, geom, axis, i)
                if base_ink is not None:
                    thr = max(CLUE_DIM_LEVEL, base_ink + 25.0)
            if ink < thr:
                bad.append(label)
    if blanks:
        # 提示是 0 的空行/空列本身天然满足，游戏通常一进关就把它们的 0 变灰，
        # 但不敢假定每个版本都这样（万一不变灰，判了就是误报）。所以只在「同一帧里
        # 既有灰又有黑」时才判：黑着的那几条就是被我读成空、实际却有提示的线——
        # 正是「灰色 0 被读成 8/6」那类错读的另一面。
        gray = [lab for lab, v in blanks if v >= CLUE_DIM_LEVEL]
        black = [lab for lab, v in blanks if v < CLUE_DIM_LEVEL]
        if gray and black:
            bad.extend(black)
    return bad


GLYPH_LIB_DIR = DEBUG_DIR / "glyph_verified"


def _harvest_verified_lines(screen: Screen, geom: Geometry, puzzle, grid: Sequence[Sequence[int]],
                            painted: Set[Tuple[int, int]], log=print) -> None:
    """把「游戏亲手验证过的行/列」存成字形样本，字形库自动积累。

    判据：这一行/列的格子按我的解已经**全涂上**（painted），并且游戏的提示**已经变灰**
    （`clue_line_ink >= CLUE_DIM_LEVEL`、且没被光标高亮）——这等于**游戏确认了这行/列的读数正确**。

    存整帧 + 这份读数与已验证线清单，离线再用**同一套裁剪逻辑**取样本。
    ⚠ 必须同一条取图路径：实测客户端抓帧与桌面截图即使棋盘尺寸完全相同（都是 20×20 @34.34px），
      逐像素差也在 0.1 以上（亚像素相位 + DWM 合成差异），跨路径做模板匹配判不出来——
      所以库只能用脚本自己的抓帧攒，不能用用户截图。
    """
    try:
        img = screen.grab_client()
        verified: List[str] = []
        for axis, n in (("R", geom.rows), ("C", geom.cols)):
            for i in range(n):
                if clue_line_highlighted(img, geom, axis, i):
                    continue                     # 测不准，不当作已验证
                cells = ([(i, c) for c in range(geom.cols) if grid[i][c] == 1]
                         if axis == "R" else
                         [(r, i) for r in range(geom.rows) if grid[r][i] == 1])
                if not cells:
                    continue                     # 空行/空列天然满足，不是"读对了"的证据
                if not all(c in painted for c in cells):
                    continue                     # 还没涂完
                inkv = clue_line_ink(img, geom, axis, i)
                if inkv is not None and inkv >= CLUE_DIM_LEVEL:
                    verified.append(f"{axis}{i + 1}")
        if not verified:
            return
        GLYPH_LIB_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        img.save(GLYPH_LIB_DIR / f"{stamp}_frame.png")
        (GLYPH_LIB_DIR / f"{stamp}_data.json").write_text(json.dumps({
            "geometry": {"rows": geom.rows, "cols": geom.cols, "cell_w": geom.cell_w,
                         "x1": geom.x1, "y1": geom.y1},
            "rows": [[int(v) for v in ln] for ln in puzzle.rows],
            "cols": [[int(v) for v in ln] for ln in puzzle.cols],
            "verified_lines": verified,
            "painted": len(painted),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"  字形库：本批有 {len(verified)} 条线经游戏确认读数正确，已存样本（{stamp}）")
    except Exception as exc:
        log(f"  [字形库] 存样本失败（不影响过关）：{exc}")


def verify_and_fix(screen: Screen, geom: Geometry, grid: Sequence[Sequence[int]],
                   before: Image.Image, cfg: Config, stop: StopController, log=print,
                   cells: Optional[Sequence[Tuple[int, int]]] = None,
                   label: str = "") -> Tuple[int, List[Tuple[int, int]]]:
    """回读画面：没变色的格子补点一次，返回（最终仍存疑数量, 存疑格子）。

    cells 给定时只验这一批（每批涂完当场验，漏涂的立刻补点——格子越小越要这样，
    20×20 的格子只有 ~34px，一次没点上就少一块）；不给就验全部目标格。
    """
    stop.sleep(0.45)
    after = screen.grab_client()
    if cells is None:
        targets = [(r, c) for r, row in enumerate(grid) for c in range(len(row)) if row[c] == 1]
    else:
        targets = list(cells)
    bad = []
    for r, c in targets:
        cx, cy = geom.cell_center(r, c)
        if _cell_diff(before, after, cx, cy) < cfg.paint_diff_threshold:
            bad.append((r, c))
    if bad:
        log(f"  {label}{len(bad)} 格看起来没填上，补点一次…")
        for r, c in bad[: int(cfg.max_reclick_cells)]:
            stop.check()
            cx, cy = geom.cell_center(r, c)
            screen.click_client(cx, cy, cfg.click_hold_ms / 1000.0)
            time.sleep(cfg.click_interval_ms / 1000.0)
        stop.sleep(0.45)
        after2 = screen.grab_client()
        still = []
        for r, c in bad:
            cx, cy = geom.cell_center(r, c)
            if _cell_diff(before, after2, cx, cy) < cfg.paint_diff_threshold:
                still.append((r, c))
        return len(still), still
    return 0, []


def _cell_is_filled(a: np.ndarray, cx: float, cy: float, half: int = 3) -> bool:
    """这一格是不是被「上色」了（青色 36,216,192 是游戏自己的填色签名）。

    只认填色：2026-10-01 真机跑特别谜题时发现，**游戏自己会给「已经凑齐的那一行/列」
    里的空格自动打叉**（灰紫 146,140,153）——按「像素变了没有」去判「多涂了」会把
    一百多格自动叉全报成误点，纯噪声。左键误点落到的格子一定是青色的，认青色就够。
    """
    x0, y0 = int(cx - half), int(cy - half)
    patch = a[max(0, y0):y0 + 2 * half + 1, max(0, x0):x0 + 2 * half + 1]
    if patch.size == 0:
        return False
    med = np.median(patch.reshape(-1, patch.shape[-1]), axis=0)
    return bool(np.abs(med - np.array([36, 216, 192], dtype=np.float64)).mean() < 46)


def find_extra_cells(screen: Screen, geom: Geometry, grid: Sequence[Sequence[int]],
                     before: Image.Image, cfg: Config, stop: StopController,
                     log=print) -> List[Tuple[int, int]]:
    """反过来查「不该有内容却给涂上了」的格子：**左键误点的唯一可见证据**。

    原来的回读只检查「该填的格子填上了没」，从不检查「该留空的格子是不是被涂了」——
    点歪一格、点到隔壁的空格上，它完全看不见。这里用同一套判据（和 before 帧比像素）
    把这类格子找出来。只报告、不自动清（清一格要拿左键循环点几次，见 README 第七节第 4 条，
    语义没在真机确认过，不乱写）。

    ⚠ 判据是「这一格现在是不是青色的填色」而不是「像素变了没有」：游戏自己会给凑齐的行/列
    自动打叉（灰紫），另外光标十字线、当前格高亮也会让空格变色——按「变了没有」判会一次
    报出一百多格假误点（2026-10-01 特别谜题真机实测 113 格全是游戏自动叉）。
    """
    stop.sleep(0.45)
    after = screen.grab_client()
    arr = np.asarray(after.convert("RGB"), dtype=np.int16)
    before_arr = np.asarray(before.convert("RGB"), dtype=np.int16)
    extra = []
    for r, row in enumerate(grid):
        for c in range(len(row)):
            if row[c] == 1:
                continue
            cx, cy = geom.cell_center(r, c)
            if _cell_is_filled(arr, cx, cy) and not _cell_is_filled(before_arr, cx, cy):
                extra.append((r, c))
    if extra:
        log(f"  警告：{len(extra)} 格「本不该填色」却被涂上了（左键误点）：{extra[:8]}")
    else:
        log("  空盘核对：没有多涂的格子（游戏自动打的叉不算）")
    return extra


# --------------------------------------------------------------------------
# 单关流程 / 连续闯关
# --------------------------------------------------------------------------


def board_signature(screen: Screen, geom: Geometry) -> str:
    """棋盘+提示区域画面的指纹（缩小后取哈希），用来判断「换关」或「过关」。"""
    img = screen.grab_client()
    x0 = max(0, int(geom.x1 - geom.max_row_slots * geom.cell_w - geom.cell_w / 2))
    y0 = max(0, int(geom.y1 - geom.max_col_slots * geom.cell_h - geom.cell_h / 2))
    x1 = min(img.width, int(geom.x2 + geom.cell_w / 2))
    y1 = min(img.height, int(geom.y2 + geom.cell_h / 2))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return ""
    crop = img.crop((x0, y0, x1, y1))
    small = crop.resize((max(1, crop.width // 4), max(1, crop.height // 4)), Image.BILINEAR)
    return hashlib.blake2b(small.tobytes(), digest_size=8).hexdigest()


def _same_board(a: Geometry, b: Geometry) -> bool:
    """两套几何参数指的是不是同一块棋盘（格数、格宽、左上格中心都吻合）。

    位置容差给到 5px：5×5 的棋盘外框很粗，自动识别出的左上格中心在不同帧之间
    会抖动 2~4px（格宽 88px，点格中心完全不受影响），容差太小会反复触发
    「重新定位＋整帧 OCR」，白白拖慢每一关。
    """
    return ((a.rows, a.cols) == (b.rows, b.cols)
            and abs(a.cell_w - b.cell_w) <= 1.0 and abs(a.cell_h - b.cell_h) <= 1.0
            and abs(a.x1 - b.x1) <= 5.0 and abs(a.y1 - b.y1) <= 5.0)


def _adopt_candidate(screen: Screen, cand: Geometry, cfg: Config, log=print) -> bool:
    """
    读一帧提示数字，判定这套几何是否可信。
    如果只差「行和 ≠ 列和」一两个数字，用总和约束 + 求解器验证自动纠错
    （高亮行/列偶尔会读错一格），避免因为一个数字就否掉正确的尺寸。
    """
    try:
        rows_c, cols_c, bad = _read_frame(screen, cand, cfg)
    except BotAbort:
        raise
    except Exception as exc:
        log(f"  {cand.rows}×{cand.cols}（格宽 {cand.cell_w:.2f}px）读取异常：{exc}")
        return False
    rows = [[c[0] for c in line] for line in rows_c]
    cols = [[c[0] for c in line] for line in cols_c]
    problems = validate_clues(rows, cols, cand)
    if problems:
        fixed = fix_by_sum_constraint(rows_c, cols_c, cand, cfg, log)
        if fixed is not None:
            rows, cols = fixed
            problems = []
    if not problems:
        # 提示条带里混进的「多余墨迹」读不出来很正常（高亮边框、界面美术）。
        # 只要读数完整通过校验，就不该被这几团墨迹否掉整块棋盘。
        bad = []
    ok = not problems and not bad
    log(f"  {cand.rows}×{cand.cols}（格宽 {cand.cell_w:.2f}px）校验{'通过 ✓' if ok else '不通过'}")
    return ok


def resolve_geometry(screen: Screen, cfg: Config, log=print,
                     attempts: int = 3) -> Optional[Geometry]:
    """
    确认当前关卡用的棋盘几何：每关都自动识别一次棋盘网格（很快），
    识别结果与配置一致就直接沿用配置（坐标是校准过的、更精确）；
    不一致说明换了关卡尺寸（5×5 ~ 20×20，全部是正方形）或者棋盘位置变了，
    这时按识别结果逐个读提示数字校验（差一两个数字会自动纠错），通过了才采用，
    避免用旧尺寸点错格子。进关瞬间画面还没稳定（动画/加载）时自动多抓几帧重试。
    """
    geom = cfg.geom
    cands: List[Geometry] = []
    warned = False
    for attempt in range(1, max(1, attempts) + 1):
        img = screen.grab_client()
        cands = auto_detect_candidates(img)
        if geom is not None:
            for c in cands:
                if _same_board(c, geom):
                    # 同一块棋盘，但识别结果的位置更贴近当前画面（抖动 2~4px）：
                    # 采用识别结果并写回配置，后续 board_present / 点格子都按它来，
                    # 免得每次都因为几像素的偏差重新定位、重读一整帧提示数字。
                    if abs(c.x1 - geom.x1) > 2.0 or abs(c.y1 - geom.y1) > 2.0:
                        cfg.geom = c
                        try:
                            cfg.save(CONFIG_PATH)
                        except Exception:
                            pass
                        return c
                    return geom
            if not warned:
                log("  棋盘与配置不一致（换关或换了尺寸），重新定位…")
                warned = True
            if not cands and board_present(screen, geom)[0]:
                return geom          # 识别不到网格但配置的棋盘确实还在，保守沿用
        if cands:
            park_cursor(screen)
            for cand in cands:
                # 游戏里的棋盘都是正方形；进关动画中间的半截网格会给出
                # 「10×15」这类非方形候选，直接跳过，别浪费一整帧 OCR
                if cand.rows != cand.cols:
                    continue
                if _adopt_candidate(screen, cand, cfg, log):
                    # 尺寸、位置或格宽跟旧的不一样 → 写回配置（换了分辨率、拖过窗口、
                    # 换过尺寸都要写）。原来只在「格数变了」时才存，结果改了窗口分辨率
                    # 后配置永远是旧的：每关都要重新定位一遍，定位一旦失败还会退回那份
                    # 按旧分辨率算出来的坐标。
                    changed = (geom is None
                               or (geom.rows, geom.cols) != (cand.rows, cand.cols)
                               or abs(geom.cell_w - cand.cell_w) > 0.5
                               or abs(geom.x1 - cand.x1) > 2.0
                               or abs(geom.y1 - cand.y1) > 2.0)
                    cfg.geom = cand
                    if changed:
                        try:
                            cfg.save(CONFIG_PATH)
                        except Exception:
                            pass
                    return cand
        if attempt < attempts:
            log(f"  画面可能还没稳定，稍后再抓一帧重试（{attempt}/{attempts}）…")
            time.sleep(0.8)
    if cands:
        log("  候选都没通过校验（可能画面不是棋盘，或提示数字被涂灰了）")
    else:
        log("  自动定位失败")
    return geom


def _bg_palette(img: Image.Image, geom: Geometry, grid: Sequence[Sequence[int]],
                limit: int = 3) -> List[Tuple[int, int, int]]:
    """用「答案里应为空」的格子估计棋盘底色（游戏是白/浅蓝交替，取出现最多的几种）。"""
    arr = np.asarray(img.convert("RGB")).astype(np.int16)
    samples = []
    for r in range(geom.rows):
        for c in range(geom.cols):
            if grid[r][c] == 1:
                continue
            cx, cy = geom.cell_center(r, c)
            patch = arr[int(cy) - 2:int(cy) + 3, int(cx) - 2:int(cx) + 3].reshape(-1, 3)
            if patch.size:
                samples.append(tuple(int(v) for v in np.median(patch, axis=0)))
    if not samples:
        return []
    cnt = Counter(tuple(v // 8 for v in s) for s in samples)
    return [tuple(int(v) * 8 + 4 for v in key) for key, _ in cnt.most_common(limit)]


def detect_prepainted(img: Image.Image, geom: Geometry, grid: Sequence[Sequence[int]]) -> int:
    """统计「答案要求填、但画面上已经有内容」的格子数（说明这关已经被人手动涂过）。"""
    palette = _bg_palette(img, geom, grid)
    if not palette:
        return 0
    arr = np.asarray(img.convert("RGB")).astype(np.int16)
    n = 0
    for r in range(geom.rows):
        for c in range(geom.cols):
            if grid[r][c] != 1:
                continue
            cx, cy = geom.cell_center(r, c)
            patch = arr[int(cy) - 2:int(cy) + 3, int(cx) - 2:int(cx) + 3].reshape(-1, 3)
            if not patch.size:
                continue
            med = np.median(patch, axis=0)
            near = min(float(np.abs(med - np.array(p, dtype=np.float64)).mean()) for p in palette)
            if near > 45:
                n += 1
    return n


def play_one(screen: Screen, geom: Geometry, cfg: Config, stop: StopController,
             confirm: bool = False, dry_run: bool = False, log=print,
             prev_key=None) -> dict:
    global _GUARD_GEOM, _STRAY_CLICKS
    geom = resolve_geometry(screen, cfg, log) or geom
    _GUARD_GEOM = geom          # 开启棋盘点击审计（只记录，不拦截）
    _STRAY_CLICKS = []
    # 注意：不要在这里用「棋盘上有多少格非底色」来提前判脏并重置。
    # 那个信号会把右击打的叉（✕）、以及光标所在行列的粉色高亮也算进去，而「重置本关」
    # 只能清掉填色、清不掉 ✕ —— 判脏 → 重置 → 仍判脏 → 再重置，会死循环。
    # 真正需要防的「这关已经被手工涂过一部分」由求解后的 detect_prepainted 精确判断
    # （只统计答案要求填、但画面上已有内容的格子），它不会误报。
    log("> 识别提示数字…")
    puzzle = read_puzzle(screen, geom, cfg, log)
    log(f"  识别成功：{puzzle.rows_n}×{puzzle.cols_n}，"
        f"行提示和={sum(puzzle.row_sums)}，列提示和={sum(puzzle.col_sums)}")
    key = puzzle.key()
    if prev_key is not None and key == prev_key:
        log("  提示数字与上一关完全相同：可能还停在同一关，本次不点击（避免重复涂格）")
        return {"filled": 0, "bad": 0, "seconds": 0.0, "dry": dry_run,
                "duplicate": True, "key": key}

    log("> 求解…")
    t0 = time.time()
    solver = PuzzleSolver(puzzle.rows, puzzle.cols)
    grid = solver.solve(cfg.solver_time_limit)
    if grid is None:
        raise BotAbort("求解失败（识别结果可能有误，建议先 --dry-run 核对提示数字）")
    cells = fill_cells(grid)
    log(f"  求解完成，用时 {time.time() - t0:.2f}s，需填 {len(cells)} 格")
    print(format_solution(grid, puzzle.rows))

    # 填色顺序：前两波合成一条蛇形铺满、试探波留到最后（有实测数据，见 fill_order_plan）——dry-run 也打出来
    plan = fill_order_plan(puzzle.rows, puzzle.cols, grid)
    log("> 填色计划：" + "、".join(f"{name} {len(w)} 格" for name, w in plan if w))

    if dry_run:
        log("  [dry-run] 不执行点击")
        return {"filled": 0, "bad": 0, "seconds": 0.0, "dry": True,
                "duplicate": False, "key": key}

    if confirm:
        log("  按 Enter 开始自动填格（F8 中止）…")
        try:
            input()
        except Exception:
            pass

    screen.ensure_foreground()
    before = screen.grab_client()
    prepainted = detect_prepainted(before, geom, grid)
    if prepainted > 0:
        log(f"  注意：检测到 {prepainted} 格已经有内容（这关已经手动涂过一部分）。")
        log("  为避免重复点击把画作弄坏，本关不动手；请先在游戏里重新开始本关，或按 F8 停止。")
        return {"filled": 0, "bad": 0, "seconds": 0.0, "dry": False,
                "duplicate": False, "key": key, "prepainted": prepainted}

    # 点之前先确认眼前不是别的界面：读题→求解这几秒里画面可能已经变成关卡列表 / 结算画作 /
    # 解锁弹窗（尤其是刚过完一关的时候），这时候按格子坐标连点几百下就是把点击打在列表方块、
    # 结算按钮或弹窗上——最糟的是打在棋盘上，把画连同不可撤销的叉一起涂坏。
    why_ui = _probably_not_board(before)
    if why_ui:
        raise BotAbort(f"准备点击前发现画面是「{why_ui}」而不是棋盘，本次一个字都没点（避免误点）。"
                       "请把这关切回棋盘界面再运行。")

    # ---- 填色顺序：蛇形铺满 + 试探波留到最后 ----
    # 一批批地涂，每批之前拿游戏自己的状态自查一次：我的解认为「该填的格子已经全涂上了」
    # 的行/列，游戏必须已经把它的提示数字变灰（judge_clue_dims）。自查不过就停在原地——
    # 宁可少涂，也别照着一个读错的题把整块画连同不可撤销的叉一起涂坏。
    # 顺序本身用 fill_order_plan：前两波合成一条蛇形（这样开头几行的提示能尽快变灰，
    # 每批自查都有料可验），只把「只能靠试探定下来的格子」留到最后（它对读题错误最敏感）。
    # 右击的叉必须在最后一格填色之前打完（填完最后一格会立刻进结算界面），
    # 所以放在最后一批之前打：副作用是这批叉也受前面几道自查的保护。
    stop.sleep(0.35)  # 给用户一点反应时间
    batch = max(8, int(cfg.fills_per_pause or 16))
    marks = empty_cells(grid) if getattr(cfg, "mark_empty", False) else []
    plan = fill_order_plan(puzzle.rows, puzzle.cols, grid)
    chunks = [(name, w[k:k + batch]) for name, w in plan for k in range(0, len(w), batch)]
    log("> 填色顺序（蛇形铺满 + 试探波留到最后）："
        + "、".join(f"{name} {len(w)} 格" for name, w in plan if w)
        + (f"；{len(marks)} 格空格的叉放在最后一批之前打" if marks else ""))
    if not chunks:
        return {"filled": 0, "bad": 0, "seconds": 0.0, "dry": False,
                "duplicate": False, "key": key}
    painted: Set[Tuple[int, int]] = set()
    cost = 0.0
    for ci, (name, chunk) in enumerate(chunks, 1):
        if painted and not getattr(screen, "is_fake", False):
            # ★ 先把光标挪到窗口角落（2026-09-30 凌晨补，真机 Lv3-028 的元凶）：
            #   刚点完的格子会让游戏把那一行/列的提示染成粉色，而自查是量提示亮度的——
            #   实测"已满足（灰）"的提示会因为高亮从 162~180 掉到 122，阈值是 108，只差 14。
            #   于是自查误报「该变灰却没变灰」，把快涂完的关卡重置掉。park_cursor 的注释
            #   早就写了"游戏会把光标所在行/列提示高亮成粉色，挪开可避免干扰"——唯独这个自查漏了。
            park_cursor(screen)
            # 刚点完的格子游戏可能还在淡入/淡出（提示变灰有个过场），先等一拍；
            # 真读出「该灰的还黑着」就再等一拍复核一次，只有两次都黑才算真不对。
            stop.sleep(0.35)
            bad = judge_clue_dims(screen.grab_client(), geom, puzzle.rows, puzzle.cols,
                                  grid, painted, baseline=before)
            if bad:
                # ★ 变灰是**渐隐动画**，不是瞬间切换：一次只等 0.5s 就下结论会误判。
                #   实测一次有 13 条线同时完成（Lv3-028 的批8），渐隐最慢，两次各 0.35/0.5s 都读到
                #   "还黑着"，于是被判成"读题错了"→ 把快涂完的关卡重置掉。
                #   改成：最多等 6 秒、每 0.6 秒复查一次，只有耗到最后一刻还黑着才算真不对。
                deadline = time.time() + 6.0
                waited = 0.0
                while bad and time.time() < deadline:
                    stop.sleep(0.6)
                    waited += 0.6
                    bad = judge_clue_dims(screen.grab_client(), geom, puzzle.rows, puzzle.cols,
                                          grid, painted, baseline=before)
                if not bad and waited:
                    log(f"  自查通过（等渐隐动画用了 {waited:.1f}s；已涂 {len(painted)} 格）")
                elif bad:
                    log(f"  等满 {waited:.1f}s 仍是黑着，判定这 {len(bad)} 条线真的对不上")
            if bad:
                # 把这几条线「我涂了什么 / 该涂什么」直接打出来：
                # 多涂了一格 → 点歪或涂错；没多涂 → 那就是提示数字读错了。
                # （Lv3-028 那次的教训：只报"没变灰"根本定位不了是哪种，白跑一整关。）
                for name in bad[:3]:
                    m = re.match(r"([行列])\s*(\d+)", str(name))
                    if not m:
                        continue
                    axis, idx = m.group(1), int(m.group(2))
                    if axis == "行":
                        line = [(idx - 1, c) for c in range(len(grid[idx - 1]))]
                    else:
                        line = [(r, idx - 1) for r in range(len(grid))]
                    want = [rc for rc in line if grid[rc[0]][rc[1]] == 1]
                    extra = [rc for rc in line if rc in painted and grid[rc[0]][rc[1]] != 1]
                    missing = [rc for rc in want if rc not in painted]
                    # 亮度值一起打出来：>=108 是"已变灰"、<108 才判"还黑着"。若它刚好卡在
                    # 108~130 之间（被高亮/底色污染），那就是测量问题而不是读题问题。
                    try:
                        inkv = clue_line_ink(screen.grab_client(), geom,
                                             "R" if axis == "行" else "C", idx - 1)
                    except Exception:
                        inkv = None
                    log(f"    {name}: 该填 {len(want)} 格、已涂 {len(want) - len(missing)} 格"
                        f"（缺 {missing[:4] if missing else '无'}）；"
                        + (f"**多涂了 {extra[:6]}** ← 点歪了" if extra else "没有多涂 ← 提示数字读错了")
                        + f"；该线提示亮度 {inkv if inkv is None else round(inkv, 1)}"
                        + f"（阈值 {CLUE_DIM_LEVEL}：低于它才判'还黑着'）")
                # ★ 反查点击落点：找出"账本说没涂完、游戏却已变灰"的线。
                #   这是区分「读题错」和「点歪了」的分水岭——读题错不会出现这种反向矛盾。
                rev = audit_click_targeting(screen.grab_client(), geom,
                                            puzzle.rows, puzzle.cols, grid, painted, log=log)
                # ★ 失败现场存档：下一批本来要涂的帧、这份读数、已涂集合。
                #   之前只存"通过"时的帧，于是失败那一刻的真实画面永远拿不到（2026-09-30 半夜的教训）。
                try:
                    if not getattr(screen, "is_fake", False):
                        GLYPH_LIB_DIR.mkdir(parents=True, exist_ok=True)
                        stamp = time.strftime("%Y%m%d_%H%M%S")
                        screen.grab_client().save(GLYPH_LIB_DIR / f"{stamp}_FAILED_frame.png")
                        (GLYPH_LIB_DIR / f"{stamp}_FAILED_data.json").write_text(json.dumps({
                            "geometry": {"rows": geom.rows, "cols": geom.cols, "cell_w": geom.cell_w,
                                         "x1": geom.x1, "y1": geom.y1},
                            "rows": [[int(v) for v in ln] for ln in puzzle.rows],
                            "cols": [[int(v) for v in ln] for ln in puzzle.cols],
                            "failed_lines": list(bad),
                            "reverse_dim": list(rev),
                            "painted": sorted(list(painted)),
                            "batch": ci, "total_batches": len(chunks),
                        }, ensure_ascii=False, indent=1), encoding="utf-8")
                        log(f"  [存档] 已把失败现场存到 debug/glyph_verified/{stamp}_FAILED_*")
                except Exception as exc:
                    log(f"  [存档] 存失败现场失败（不影响流程）：{exc}")
                raise BotAbort(
                    f"自查没通过：{'、'.join(bad[:6])} 这些线「该填的格子已经涂完，游戏却没把提示变灰」"
                    f"——说明提示数字读错了、或者格子涂到了别处。已涂 {len(painted)} 格，剩下的没敢再涂。"
                    f"建议先看一眼这几条线的提示数字，或用 --dry-run 核对。")
            log(f"  自查通过（已涂 {len(painted)} 格，游戏那边的提示灰度对得上）")
            if not getattr(screen, "is_fake", False):
                # 自查通过 = 这几条线的读数被游戏亲手确认了 → 把样本攒进字形库（见 _harvest_verified_lines）
                _harvest_verified_lines(screen, geom, puzzle, grid, painted, log)
        if marks and ci == len(chunks):
            log(f"> 右键打叉：{len(marks)} 格…")
            mark_cost = execute_marks(screen, geom, marks, cfg, stop, log)
            log(f"  打叉完成，用时 {mark_cost:.2f}s")
            marks = []
        log(f"> 涂第 {ci}/{len(chunks)} 批（{name} {len(chunk)} 格）…")
        cost += execute_fill(screen, geom, chunk, cfg, stop, log)
        painted.update(chunk)
        # 每批涂完当场回读这一批：格子小的时候（20×20 只有 ~34px）一次没点上就是少一块，
        # 当场补点比等到收尾再补便宜得多，也让日志能报出真实的漏点/误点次数。
        miss, miss_cells = verify_and_fix(screen, geom, grid, before, cfg, stop, log,
                                          cells=chunk, label=f"第 {ci} 批：")
        if miss:
            log(f"  第 {ci} 批补点后仍有 {miss} 格存疑 {miss_cells[:6]}")
    bad_cnt, bad_cells = verify_and_fix(screen, geom, grid, before, cfg, stop, log)
    if bad_cnt:
        log(f"  警告：{bad_cnt} 格回读仍有疑问 {bad_cells[:8]}")
    if not getattr(screen, "is_fake", False):
        park_cursor(screen)      # 同自查：收尾回读/diff 也别在光标停在棋盘上时做
    find_extra_cells(screen, geom, grid, before, cfg, stop, log)
    if _STRAY_CLICKS:
        log(f"  审计：本轮有 {len(_STRAY_CLICKS)} 次点击落在棋盘格上、但不是计划内的填色/打叉："
            f"{[(r + 1, c + 1) for _x, _y, (r, c) in _STRAY_CLICKS][:8]}"
            f"（上面那行的行/列号从 1 数；人眼看到的「误点」就是这种）")
    else:
        log("  审计：本轮所有落在棋盘上的点击都是计划内的格子（零 stray 点击）")
    _GUARD_GEOM = None
    log(f"  填格完成：点击 {len(painted)} 次，用时 {cost:.2f}s")
    return {"filled": len(painted), "bad": bad_cnt, "seconds": cost, "dry": False,
            "duplicate": False, "key": key}


def wait_until_changed(screen: Screen, geom: Geometry, cfg: Config, stop: StopController,
                       baseline: str, timeout: float, log=print) -> bool:
    """等待画面指纹变化（换关 / 过关动画 / 结算界面都会触发）。"""
    end = time.time() + timeout
    last_msg = 0.0
    while time.time() < end:
        stop.check()
        if board_signature(screen, geom) != baseline:
            return True
        if time.time() - last_msg > 5:
            last_msg = time.time()
            log("  等待本关结束（画面变化）…")
        stop.sleep(0.5)
    return False


def _board_box(geom: Geometry) -> Tuple[float, float, float, float]:
    """棋盘 + 提示数字面板的包围盒。

    只比较这个区域就能排除一直在动的角色动画/计时器等，避免「棋盘明明在、
    却因为窗外的东西在动」而永远判定不出一块稳定的棋盘。
    """
    left = geom.x1 - geom.max_row_slots * geom.cell_w - geom.cell_w
    top = geom.y1 - geom.max_col_slots * geom.cell_h - geom.cell_h
    right = geom.x2 + geom.cell_w
    bottom = geom.y2 + geom.cell_h
    return left, top, right, bottom


def _board_area_gray(img: Image.Image, geom: Geometry) -> np.ndarray:
    """棋盘+提示面板区域的小灰图（判断画面是否已经稳定用）。"""
    x0, y0, x1, y1 = _board_box(geom)
    x0 = max(0, int(x0))
    y0 = max(0, int(y0))
    x1 = min(img.width, int(x1))
    y1 = min(img.height, int(y1))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return np.zeros((1, 1), dtype=np.float32)
    g = img.crop((x0, y0, x1, y1)).convert("L").resize((96, 54), Image.BILINEAR)
    return np.asarray(g, dtype=np.float32)


def _wait_board_stable(screen: Screen, geom: Geometry, timeout: float = 2.5,
                       tol: float = 0.008) -> bool:
    """等棋盘区域不再变化（进关动画、高亮闪烁停下来）再开始 OCR。

    刚进关卡时提示数字有出现动画，抢在那时候读多半读到半截数字——典型症状
    是某一列读出个 37、行列总和对不上，白跑好几帧 OCR 还判不出结果。
    """
    prev: Optional[np.ndarray] = None
    end = time.time() + timeout
    while time.time() < end:
        try:
            cur = _board_area_gray(screen.grab_client(), geom)
        except BotAbort:
            raise
        except Exception:
            time.sleep(0.2)
            continue
        if (prev is not None and prev.shape == cur.shape
                and float(np.abs(cur - prev).mean()) / 255.0 < tol):
            return True
        prev = cur
        time.sleep(0.15)
    return False


def _screen_changed_pct(a: Image.Image, b: Image.Image,
                        box: Optional[Tuple[float, float, float, float]] = None) -> float:
    """两帧画面里变化像素的比例（用于确认点击是否生效）。

    给了 box 就只看这一块区域。
    """
    if a.size != b.size:
        return 1.0
    if box is not None:
        x0 = max(0, int(box[0])); y0 = max(0, int(box[1]))
        x1 = min(a.width, int(box[2])); y1 = min(a.height, int(box[3]))
        if x1 - x0 >= 2 and y1 - y0 >= 2:
            a = a.crop((x0, y0, x1, y1))
            b = b.crop((x0, y0, x1, y1))
    aa = np.asarray(a.convert("L"), dtype=np.float32)
    bb = np.asarray(b.convert("L"), dtype=np.float32)
    if aa.size == 0:
        return 0.0
    return float((np.abs(aa - bb) > 24).mean())


def _ui_point(screen: Screen, x: float, y: float) -> Tuple[float, float]:
    """关卡内 UI 坐标按 1280×720 客户区实测；窗口尺寸变了就按比例换算。

    算出来的点会记进 `_UI_POINTS`：棋盘点击审计用它来区分「这是我们自己的 UI 按钮」
    和「不知从哪来的一下」。
    """
    try:
        w, h = client_size(screen.hwnd)
        if w >= 100 and h >= 100:
            pt = x * w / 1280.0, y * h / 720.0
            _UI_POINTS.append(pt)
            return pt
    except Exception:
        pass
    _UI_POINTS.append((x, y))
    return x, y


def _board_line_visible(img: Image.Image, geom: Optional[Geometry]) -> bool:
    """这一帧里还看得见「我们这套几何对应的棋盘」吗？

    只看网格线，不做 OCR、不看格子内容——所以涂了一半、打满叉、提示数字变灰、光标高亮
    都照样认得出来。用途：在做「兜底点一下」这种盲点之前确认眼前不是棋盘。
    """
    if geom is None:
        return False
    try:
        return any(_same_board(c, geom) for c in auto_detect_candidates(img))
    except BotAbort:
        raise
    except Exception:
        return False


def _safe_ui_point(screen: Screen, geom: Optional[Geometry],
                   x: float, y: float) -> Tuple[float, float]:
    """「点一下继续」这类盲点用的坐标：按窗口尺寸换算后，保证不落在棋盘（含提示面板）范围内。

    1280×720 时代把 (640,480) 当画面中央，换成 1920×1080 后按比例换算成 (960,720)，
    正好落在棋盘正中间——只要「界面认不出来就点一下中央」的兜底在棋盘还亮着的时候
    触发一次，就会把那一格涂花（实测两个分辨率都落在棋盘框内）。这里把落在棋盘框里的
    点挪到棋盘外的空档：结算/弹窗界面整屏都能点，位置不敏感。
    """
    px, py = _ui_point(screen, x, y)
    if geom is None:
        return px, py
    try:
        w, h = client_size(screen.hwnd)
    except Exception:
        return px, py
    if w < 100 or h < 100:
        return px, py
    left, top, right, bottom = _board_box(geom)
    if not (left - 8 <= px <= right + 8 and top - 8 <= py <= bottom + 8):
        return px, py                      # 本来就在棋盘外，不用挪
    mid_y = min(max((top + bottom) / 2.0, 40.0), h - 40.0)
    mid_x = min(max(px, 40.0), w - 40.0)
    gaps: List[Tuple[float, float, float]] = []
    if left - 8 >= 60:
        gaps.append((left - 8, (left - 8) / 2.0, mid_y))
    if w - (right + 8) >= 60:
        gaps.append((w - (right + 8), right + 8 + (w - (right + 8)) / 2.0, mid_y))
    if top - 8 >= 50:
        gaps.append((top - 8, mid_x, (top - 8) / 2.0))
    if h - (bottom + 8) >= 50:
        gaps.append((h - (bottom + 8), mid_x, bottom + 8 + (h - (bottom + 8)) / 2.0))
    if not gaps:
        # 棋盘几乎铺满整屏，实在没有安全位置：交给调用方用 _board_line_visible 挡掉
        return px, py
    gaps.sort(reverse=True)
    return gaps[0][1], gaps[0][2]


def _probably_not_board(img: Image.Image) -> str:
    """这一帧看起来「肯定不是某一关的棋盘」吗？返回界面类型（空串 = 不像这些界面）。

    给「马上要连点几百格」的地方当闸门：读题→求解这几秒里画面可能已经变成关卡列表 /
    结算画作 / 解锁弹窗，这时候按格子坐标连点就是把点击打到列表方块、结算按钮或弹窗上
    （最糟的是打到棋盘上，把画涂坏）。
    """
    if _is_level_list(img):
        return "关卡列表"
    if _is_result_screen(img):
        return "结算/画作界面"
    if _looks_dark(img):
        return "暗色遮罩（过关结算/解锁弹窗）"
    return ""


def _click_next_button(screen: Screen, cfg: Config, stop: StopController, log=print) -> bool:
    """点一下校准里记录的「下一关/继续」按钮（没有记录就跳过）。"""
    if not cfg.next_button:
        return False
    log("  点击已记录的「下一关/继续」按钮…")
    screen.ensure_foreground()
    stop.check()
    screen.click_client(*cfg.next_button)
    stop.sleep(cfg.next_button_delay)
    return True


# 过关弹窗「新的画作已解锁！」在 1280×720 客户区里的橙色「返回」按钮（实测包围盒）
_UNLOCK_BTN = (491.0, 473.0)
_UNLOCK_SCAN = (330.0, 448.0, 660.0, 502.0)


def _has_orange_button(img: Image.Image) -> bool:
    """扫描区里有没有大片纯橙色（棋盘/关卡列表/主菜单上都不会出现）。"""
    fx0, fy0, fx1, fy1 = _UNLOCK_SCAN
    w, h = img.size
    x0, y0 = max(0, int(fx0 * w / 1280)), max(0, int(fy0 * h / 720))
    x1, y1 = min(w, int(fx1 * w / 1280)), min(h, int(fy1 * h / 720))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return False
    arr = np.asarray(img.crop((x0, y0, x1, y1)).convert("RGB"), dtype=np.int16)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    orange = (r > 200) & (g > 105) & (g < 205) & (b < 110)
    return float(orange.mean()) > 0.35


def dismiss_unlock_popup(screen: Screen, cfg: Config, stop: StopController, log=print) -> bool:
    """过关后若弹出「新的画作已解锁！」，点橙色「返回」把它关掉。"""
    try:
        if not _has_orange_button(screen.grab_client()):
            return False
        x, y = _ui_point(screen, *_UNLOCK_BTN)
        log("  点掉过关弹窗「返回」…")
        screen.ensure_foreground()
        stop.check()
        screen.click_client(x, y)
        stop.sleep(0.9)
        return True
    except BotAbort:
        raise
    except Exception as exc:
        log(f"  关闭过关弹窗失败：{exc}")
        return False


# 「普通谜题」关卡列表：3×5 方块中心（1280×720 客户区实测），未完成的关卡显示灰色「?」
LIST_X = (351.0, 495.0, 639.0, 783.0, 927.0)
LIST_Y = (188.0, 296.0, 404.0)
_CONTINUE_POINT = (640.0, 480.0)
# 列表右侧的黄色「G」大箭头：翻下一页（到头时游戏会把箭头藏起来）
PAGE_RIGHT_POINT = (1190.0, 300.0)
# 难度页签 Lv1/Lv2/Lv3（点页签会回到该页签第 1 页）
TAB_POINTS = ((1, 879.0, 30.0), (2, 1006.0, 30.0), (3, 1129.0, 30.0))
# 翻页判据：同一页时缩略图区域几乎不变（实测 0.1%），真正翻页 ≥3.5%
LIST_FLIP_MIN_DIFF = 0.012


def _yellow_ratio(img: Image.Image) -> float:
    """关卡列表里那张浅黄卡片底色的占比（只在卡片范围内统计）。

    注意不能看整屏：关卡内的画面也有大片黄色（左下角的人物、右下角的「标记」
    按钮），整屏统计会把「关卡里的棋盘」也当成列表。只看列表卡片所在的这块
    区域就非常干净——列表 ≈0.50，关卡内 ≈0.00。
    """
    w, h = img.size
    x0, y0 = int(305 * w / 1280), int(100 * h / 720)
    x1, y1 = int(985 * w / 1280), int(460 * h / 720)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return 0.0
    a = np.asarray(img.crop((x0, y0, x1, y1)).convert("RGB"), dtype=np.int16)
    if a.size == 0:
        return 0.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    m = (r > 240) & (g > 232) & (b > 150) & (b < 245)
    return float(m.mean())


# --------------------------------------------------------------------------
# 关卡列表：两种布局（普通谜题 / 特别谜题）
# --------------------------------------------------------------------------
#
# 普通谜题 = 中间一块「浅黄卡片 5×3」；特别谜题（拼图）外面那一圈布局完全不同：
#   - 左边信息面板（Lv1 / 总计 / 已完成任务 n/75）+ 左下的任务栏，
#   - 右边一块 **5×5 的灰色「?」方块阵**（解开过的格子显示画作像素，不再是灰挡板），
#   - 底部 8 个页码圆点，右侧橙色「G」翻页箭头的位置也不同（特别谜题在下面 SPECIAL_* 里）。
# 实测（1920×1080 客户区）：方块阵 x1112..1651 / y288..827，格距 108px，5 列 × 5 行；
# 箭头中心 (1857,540)。下面按 1280×720 基准记录（= 实测值 ÷1.5），再按窗口尺寸换算——
# 和 LIST_X/LIST_Y/PAGE_RIGHT_POINT 一个路子。
SPECIAL_GRID_X = 741.3          # 方块阵左上角（基准）
SPECIAL_GRID_Y = 192.0
SPECIAL_GRID_PITCH = 72.0       # 格距（基准）
SPECIAL_GRID_COLS = 5
SPECIAL_GRID_ROWS = 5
SPECIAL_GRID_BOX = (SPECIAL_GRID_X, SPECIAL_GRID_Y,
                    SPECIAL_GRID_X + SPECIAL_GRID_PITCH * SPECIAL_GRID_COLS,
                    SPECIAL_GRID_Y + SPECIAL_GRID_PITCH * SPECIAL_GRID_ROWS)
SPECIAL_PAGE_RIGHT_POINT = (1238.0, 360.0)      # 右侧橙色「G」箭头（普通列表是 1190,300）
# 自动识别方阵内部「格线明显」的条数下限（内部共 8 条：4 竖 + 4 横）。
# 每条线的「明显程度」= 这条线上**逐点**亮度差绝对值的平均（见 `_special_line_values`，
# 千万别改成「先平均再取绝对值」）。
# 实测（2026-10-01，逐点绝对差）：
#   特别谜题选关界面（解开 0 / 7 / 15 / 16 格都测过）：明显 8/8，最弱那条 12.5~21.6，检出棋盘 0
#   关卡内棋盘（18 张实拍）：明显 3~7/8，最弱 0.3~7.6，检出棋盘 1
#   关卡内「关卡完成」那一屏（棋盘填满）：明显 8/8，最弱 10.2，检出棋盘 1 ← 要靠这两条挡住
#   普通谜题列表：明显 5/8，最弱 ≤0.8，检出棋盘 0
# 所以三条一起判：明显条数 ≥5、8 条里最弱 >11、画面里检不出棋盘。
SPECIAL_MIN_LINES_WITH_PLATES = 5
SPECIAL_LINES_WEAKEST = 11.0
# 有这么多「灰挡板」格就够认这一屏了（棋盘实测 0 个）—— 现在是辅助判据，见 `_special_tile_field_lines`
SPECIAL_MIN_PLATES = 1
# 方块阵 25 格里「黄底」的格子数上限：铺满整块的方块阵最多只有零星几格露底色
# （实测特别谜题 1 格、普通谜题列表 12 格、关卡内 0 格）。
SPECIAL_MAX_BG = 9

LIST_MODE_AUTO = "auto"
LIST_MODE_NORMAL = "normal"
LIST_MODE_SPECIAL = "special"
LIST_MODES = (LIST_MODE_AUTO, LIST_MODE_NORMAL, LIST_MODE_SPECIAL)
_LIST_MODE = LIST_MODE_AUTO
# 最近一次认出来的列表类型。特别谜题的某一页 25 格全解开后，方块阵里一个灰挡板都不剩，
# 光看画面分不出「这一页打完了」和「棋盘」——这时用上一次认出的类型兜底，
# 才知道该去翻页，而不是去点棋盘。
_LAST_LIST_KIND = ""
# 「上一次报给用户看的布局」——只在**变了**的时候打一行日志（见 `_note_layout`）。
# 用户在面板日志里就能看出「它现在按哪种列表在干活 / 什么时候换的」。
_LAST_LOGGED_KIND = ""


_LAYOUT_HUMAN = {
    LIST_MODE_SPECIAL: "特别谜题（5×5 灰「?」方块阵）",
    LIST_MODE_NORMAL: "普通谜题（浅黄卡片）",
}


def _note_layout(kind: str, log=print) -> None:
    """画面里的关卡列表布局**变了**就报一行（第一次认出来也报）。

    用户在面板日志里一眼就能看出「切换了没有」——不用去猜、也不用翻配置：
      [布局] 当前画面：特别谜题（5×5 灰「?」方块阵）
    认不出来（""）不报，免得刷屏。
    """
    global _LAST_LOGGED_KIND
    if not kind or kind == _LAST_LOGGED_KIND:
        return
    _LAST_LOGGED_KIND = kind
    log(f"[布局] 当前画面：{_LAYOUT_HUMAN.get(kind, kind)}")


def set_list_mode(mode: Optional[str]) -> str:
    """设置「关卡列表布局」：auto / normal / special（见 --list-mode 说明）。"""
    global _LIST_MODE
    m = (mode or LIST_MODE_AUTO).strip().lower()
    _LIST_MODE = m if m in LIST_MODES else LIST_MODE_AUTO
    return _LIST_MODE


def get_list_mode() -> str:
    return _LIST_MODE


def _special_grid_probe(img: Image.Image) -> List[Tuple[int, int, float, float, float, float]]:
    """把「特别谜题」方块阵的 25 格逐格量一遍。

    每格采内部 68% 那一小块（避开格子边框），返回
    (行, 列, 中心x, 中心y, 灰挡板占比, 黄底占比)。坐标是客户区像素。
    """
    a = np.asarray(img.convert("RGB"), dtype=np.int16)
    h, w = a.shape[:2]
    sx, sy = w / 1280.0, h / 720.0
    px, py = SPECIAL_GRID_PITCH * sx, SPECIAL_GRID_PITCH * sy
    hw, hh = px * 0.34, py * 0.34
    out: List[Tuple[int, int, float, float, float, float]] = []
    for r in range(SPECIAL_GRID_ROWS):
        for c in range(SPECIAL_GRID_COLS):
            cx = SPECIAL_GRID_X * sx + px * (c + 0.5)
            cy = SPECIAL_GRID_Y * sy + py * (r + 0.5)
            x0, y0 = int(round(cx - hw)), int(round(cy - hh))
            x1, y1 = int(round(cx + hw)), int(round(cy + hh))
            if x0 < 0 or y0 < 0 or x1 > w or y1 > h or x1 - x0 < 4 or y1 - y0 < 4:
                out.append((r, c, cx, cy, -1.0, -1.0))
                continue
            s = a[y0:y1, x0:x1]
            mx, mn = s.max(axis=2), s.min(axis=2)
            g = s.mean(axis=2)
            r_ch, b_ch = s[..., 0], s[..., 2]
            # 灰「?」挡板：中性灰（三个通道几乎相等）、亮度 150~216（实测挡板 ~187）
            plate = ((mx - mn) < 22) & (g > 150) & (g < 216)
            # 界面黄底（浅黄卡片/背景），用来判断「这一格根本不是方块阵里的格子」
            bg = (r_ch > 235) & (s[..., 1] > 225) & (b_ch > 150) & (b_ch < 245)
            out.append((r, c, cx, cy, float(plate.mean()), float(bg.mean())))
    return out


def detect_special_grid(img: Image.Image) -> Optional[Dict[str, object]]:
    """眼前这帧是不是「特别谜题」的关卡方块阵？是就返回方块阵信息，不是返回 None。

    返回 {'plates': [(行,列,中心x,中心y), ...],   # 还没解开的格子（灰「?」挡板）
          'n_plate': n, 'n_bg': m, 'cells': [(行,列,中心x,中心y,挡板占比,黄底占比), ...]}
    判据只来自画面本身：方块阵把 5×5 那一整块铺满 → 25 格里「黄底」的必须很少；
    同时要有「灰挡板」的格子（没解开的关卡）。
    """
    cells = _special_grid_probe(img)
    n_bg = sum(1 for _r, _c, _x, _y, _p, b in cells if b >= 0.6)
    plates = [(r, c, x, y) for r, c, x, y, p, b in cells if p >= 0.5]
    return {"plates": plates, "n_plate": len(plates), "n_bg": n_bg, "cells": cells}


def _special_line_values(img: Image.Image) -> List[float]:
    """8 条内部格线各自的「明显程度」= 该线上**逐点**亮度差的绝对值的平均。

    ⚠ 必须逐点取绝对值，不能「先平均再取绝对值」：同一条线上既有画作格又有灰挡板格时，
    画作那段的差是**负的**（画作比相邻格亮），挡板那段是正的，先平均会互相抵消 —— 实测解到
    16 格时同一条横线「先平均」只剩 4.0，而逐点绝对差是 12.5。这就是真机上「一排完了不换行」
    外加「过关后认不出回到方块阵」的直接原因（一个根因两个症状）。
    """
    a = np.asarray(img.convert("L"), dtype=np.float32)
    h, w = a.shape
    sx, sy = w / 1280.0, h / 720.0
    px, py = SPECIAL_GRID_PITCH * sx, SPECIAL_GRID_PITCH * sy
    x0, y0 = SPECIAL_GRID_X * sx, SPECIAL_GRID_Y * sy
    # 只看方块阵内部：去掉最外面半格，免得把界面边缘当成格线
    xa, xb = int(x0 + px * 0.25), int(x0 + px * (SPECIAL_GRID_COLS - 0.25))
    ya, yb = int(y0 + py * 0.25), int(y0 + py * (SPECIAL_GRID_ROWS - 0.25))
    if xb - xa < 8 or yb - ya < 8:
        return []
    off = max(3, int(px * 0.06))
    vals: List[float] = []
    for k in range(1, SPECIAL_GRID_COLS):           # 竖着的格线
        x = int(round(x0 + px * k))
        if not (xa < x < xb) or x - off <= 0 or x + 1 + off >= w:
            continue
        step = a[ya:yb, x] - (a[ya:yb, x - off:x].mean(axis=1)
                              + a[ya:yb, x + 1:x + 1 + off].mean(axis=1)) / 2.0
        vals.append(float(np.mean(np.abs(step))))
    for k in range(1, SPECIAL_GRID_ROWS):           # 横着的格线
        y = int(round(y0 + py * k))
        if not (ya < y < yb) or y - off <= 0 or y + 1 + off >= h:
            continue
        step = a[y, xa:xb] - (a[y - off:y, xa:xb].mean(axis=0)
                              + a[y + 1:y + 1 + off, xa:xb].mean(axis=0)) / 2.0
        vals.append(float(np.mean(np.abs(step))))
    return vals


def _special_boundary_lines(img: Image.Image) -> Tuple[int, int]:
    """方块阵内部那 8 条「格子分隔线」里，有几条是明显的？（明显 = 那条线的逐点绝对差平均值 >8）

    这是**正面证据**：特别谜题的格子是画在网格线上的（灰挡板之间是 149 的分隔线、画作格
    之间是一条更亮的、很淡的分隔线），所以不管这一页解开多少格，8 条内部格线都在。
    实测（逐点绝对差）：特别谜题选关界面 8/8（20~25），关卡内棋盘 ≤7/8（最弱那条 0.3~7.6），
    普通谜题列表 3~5/8，而**关卡内「关卡完成」那一屏**（棋盘填满）也是 8/8 ——
    所以「一个灰挡板都没有」时还要靠 `_special_line_weakest` + 不许是棋盘这两条一起判。
    """
    vals = _special_line_values(img)
    return sum(1 for v in vals if v > 8.0), len(vals)


def _special_line_weakest(img: Image.Image) -> float:
    """8 条格线里最弱的那条有多明显（一个灰挡板都没有的那一页要靠它认）。"""
    vals = _special_line_values(img)
    return min(vals) if vals else 0.0


def _special_tile_field_lines(img: Image.Image,
                              info: Optional[Dict[str, object]] = None) -> bool:
    """眼前是不是特别谜题的那块 5×5 方块阵？（三条正面判据一起过才算）

    ① 8 条内部格线里明显的 ≥ `SPECIAL_MIN_LINES_WITH_PLATES` 条；
    ② 8 条里**最弱**的那条 > `SPECIAL_LINES_WEAKEST` —— 这条是关键：解开几格之后画作与画作
       之间的分隔线很淡（一个灰挡板都没有时只剩最弱 ~12），而关卡内「关卡完成」那一屏（棋盘
       填满）最弱只有 10.2，正好被这条挡在外面；
    ③ 画面里**检不出棋盘**（`auto_detect_candidates`）——「关卡完成」那一屏棋盘还在眼前，
       棋盘自己的格线会叠在这套坐标上，光看格线是分不开的，必须看有没有棋盘。

    info 可以复用调用方已经算好的方块阵统计（省一次 25 格扫描）。
    """
    strong, total = _special_boundary_lines(img)
    if total < 4 or strong < SPECIAL_MIN_LINES_WITH_PLATES:
        return False
    if _special_line_weakest(img) <= SPECIAL_LINES_WEAKEST:
        return False
    try:
        if auto_detect_candidates(img):
            return False
    except BotAbort:
        raise
    except Exception:
        pass
    if info is None:
        info = detect_special_grid(img)
    if info is None or int(info["n_bg"]) > SPECIAL_MAX_BG:
        return False
    if int(info["n_plate"]) < SPECIAL_MIN_PLATES and strong < total:
        # 一个灰挡板都没有（这一页基本打完了）：这种时候要求 8 条全明显，别把「棋盘填满」那种
        # 只剩一点点线索的画面放进来
        return False
    return True


def _list_kind(img: Image.Image) -> str:
    """这一帧是哪种关卡列表：LIST_MODE_NORMAL / LIST_MODE_SPECIAL / ""（不是列表）。

    判据都来自画面自己（见 `_special_tile_field_lines` / `_yellow_ratio`）。认不出就当「不是列表」
    ——**绝不退回普通列表去点**：两套布局的坐标完全对不上，那一下就点到方块阵甚至棋盘上了。
    强制 normal 时也一样：方块阵在眼前就一个字都不点。
    """
    global _LAST_LIST_KIND
    if _LIST_MODE == LIST_MODE_NORMAL:
        return "" if _special_tile_field_lines(img) else (
            LIST_MODE_NORMAL if _yellow_ratio(img) >= 0.35 else "")
    if _special_tile_field_lines(img):
        _LAST_LIST_KIND = LIST_MODE_SPECIAL
        return LIST_MODE_SPECIAL
    if _LIST_MODE != LIST_MODE_SPECIAL and _yellow_ratio(img) >= 0.35:
        _LAST_LIST_KIND = LIST_MODE_NORMAL
        return LIST_MODE_NORMAL
    return ""


def _special_grid_gray(img: Image.Image) -> np.ndarray:
    """特别谜题方块阵区域的小灰图（判断「翻页是否真的发生了」用）。"""
    return _region_gray(img, SPECIAL_GRID_BOX)


def _avoid_has(pt: Tuple[float, float], tol: float = 3.0) -> bool:
    """跳过表里有没有这个位置（特别谜题格子的坐标是量出来的，留一点容差）。"""
    if pt in _AVOID_PTS:
        return True
    return any(abs(px - pt[0]) <= tol and abs(py - pt[1]) <= tol for px, py in _AVOID_PTS)


def _note_read_fail(pt: Optional[Tuple[float, float]], prev: int = 0) -> int:
    """记一笔「这一格读不通」，返回它**跨轮累计**栽了几次。

    见 `_READ_FAIL_BY_PT` 的注释：`play_one` 一轮只跑一次，第一次失败会重置本关后
    `continue`（这一轮就结束），所以只看 one-call 内的计数器会永远停在「第 1 次」，
    「第 2 次仍读不通就跳过」那条分支永远走不到 —— 真机表现就是死磕同一格。
    """
    if pt is None:
        return prev + 1
    _READ_FAIL_BY_PT[pt] = _READ_FAIL_BY_PT.get(pt, 0) + 1
    return max(prev + 1, _READ_FAIL_BY_PT[pt])


def _forget_read_fail(pt: Optional[Tuple[float, float]]) -> None:
    """这一格读通了/已经处理完了，把它的失败账划掉。"""
    if pt is not None:
        _READ_FAIL_BY_PT.pop(pt, None)


def _is_level_list(img: Image.Image) -> bool:
    """当前画面是不是「关卡列表」（普通谜题 / 特别谜题都算）。

    特别谜题的选关界面黄底也过 _yellow_ratio（实测 0.457），所以必须先认方块阵，
    认出来就当它是列表、不再按普通列表的坐标去点。
    """
    return _list_kind(img) != ""


def _navy_bar_ratio(img: Image.Image) -> float:
    """底部信息栏（深蓝色横条，带「任务/无提示/无失误」）的占比。

    过关后的「画作展示」界面和关卡列表都有这条栏，关卡内（含进关动画、
    正在打的棋盘）没有——用它来认「这一关已经打完了」。
    """
    w, h = img.size
    x0, y0 = int(245 * w / 1280), int(500 * h / 720)
    x1, y1 = int(1010 * w / 1280), int(600 * h / 720)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return 0.0
    a = np.asarray(img.crop((x0, y0, x1, y1)).convert("RGB"), dtype=np.int16)
    r, b = a[..., 0], a[..., 2]
    navy = (b > 70) & ((b - r) > 30) & (r < 110)
    return float(navy.mean())


def _is_result_screen(img: Image.Image) -> bool:
    """过关后的「画作展示」界面：整幅画 + 底部关卡信息栏（但不是列表）。

    实测：画作展示的底部深蓝占比 0.61，关卡内只有 0.06（列表是 0.56，但会被
    _is_level_list 先挑走）。认出它就能立刻点「继续」，不用瞎等好几秒。
    """
    return (not _is_level_list(img)) and _navy_bar_ratio(img) >= 0.25


def _gold_ring_ratio(img: Image.Image, cx: float, cy: float,
                     r_out: float = 44.0, r_in: float = 34.0) -> float:
    """方块外圈那圈彩色描边的占比（金色边框，或选中态的青色高亮边框）。

    已完成的关卡方块是「金边 + 缩略图」，未完成的是「灰边 + 灰问号」。
    注意：被选中的那个方块整圈会换成青色高亮，金边占比直接从 0.37 掉到 0.00
    （实测青边 0.25~0.65）——只认金色的话，选中的关卡（往往正是刚才进去过、
    没满三星的那一关）会被当成空格子跳过，重打分支就永远不触发。所以两种颜色都算。
    只看中心灰度是不够的：过关后如果画作本身是灰白的（例如 Lv1-020「老鼠」），
    缩略图整块都是灰的，会被当成还没打过、于是反复去点同一关。
    """
    w, h = img.size
    x, y = int(cx * w / 1280.0), int(cy * h / 720.0)
    ro = int(r_out * w / 1280.0)
    ri = int(r_in * w / 1280.0)
    R = ro + 2
    box = (x - R, y - R, x + R + 1, y + R + 1)
    if box[0] < 0 or box[1] < 0 or box[2] > w or box[3] > h:
        return 0.0
    a = np.asarray(img.crop(box).convert("RGB"), dtype=np.int16)
    if a.size == 0:
        return 0.0
    yy, xx = np.mgrid[0:a.shape[0], 0:a.shape[1]]
    dist = np.sqrt((xx - R) ** 2 + (yy - R) ** 2)
    ring = (dist >= ri) & (dist <= ro)
    if not ring.any():
        return 0.0
    sub = a[ring]
    r, g, b = sub[:, 0], sub[:, 1], sub[:, 2]
    gold = (r > 170) & (g > 95) & (g < 195) & (b < 95)
    cyan = (b > 150) & (g > 140) & (r < 140)
    return float((gold | cyan).mean())


def _is_question_block(img: Image.Image, cx: float, cy: float) -> bool:
    """方块中心区域是不是灰色的「?」（未完成的关卡）。"""
    # 有金色描边 = 已经打过了（缩略图），不可能是「?」。
    # 阈值压到 0.06：光标停在上面的方块会被蓝色高亮框遮掉一部分金边
    # （实测金边占比从 0.37 掉到 0.09~0.12），但未完成的灰方块始终是 0.00。
    if _gold_ring_ratio(img, cx, cy) >= 0.06:
        return False
    w, h = img.size
    x, y = int(cx * w / 1280.0), int(cy * h / 720.0)
    r = max(6, int(26 * w / 1280.0))
    x0, y0 = max(0, x - r), max(0, y - r)
    x1, y1 = min(w, x + r), min(h, y + r)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return False
    a = np.asarray(img.crop((x0, y0, x1, y1)).convert("RGB"), dtype=np.int16)
    r, b = a[..., 0], a[..., 2]
    mx, mn = a.max(axis=2), a.min(axis=2)
    # 「?」方块是中等灰（约 150~200）；浅黄底、白色/浅蓝棋盘格、彩色图案都不算
    gray = ((mx - mn) < 28) & ((r - b) < 12) & (mn > 120) & (mn < 222)
    return float(gray.mean()) > 0.6


def _region_gray(img: Image.Image, box: Tuple[float, float, float, float]) -> np.ndarray:
    """把客户区里某一块（1280×720 基准坐标）缩成小灰图，用来判断「这一块变了没有」。"""
    w, h = img.size
    crop = (int(box[0] * w / 1280), int(box[1] * h / 720),
            int(box[2] * w / 1280), int(box[3] * h / 720))
    g = img.crop(crop).convert("L").resize((72, 28), Image.BILINEAR)
    return np.asarray(g, dtype=np.float32)


# 普通谜题列表的「缩略图区域」（1280×720 基准）
LIST_THUMB_BOX = (305.0, 100.0, 985.0, 460.0)


def _list_thumb_gray(img: Image.Image) -> np.ndarray:
    """关卡列表里 3×5 缩略图区域的小灰图（判断「翻页是否真的发生了」用）。"""
    return _region_gray(img, LIST_THUMB_BOX)


def _thumb_diff(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return 1.0
    return float(np.abs(a - b).mean()) / 255.0


def _flip_list_page(screen: Screen, stop: StopController, pt: Tuple[float, float],
                    log=print, tries: int = 2,
                    box: Optional[Tuple[float, float, float, float]] = None) -> bool:
    """点一下列表两侧的翻页箭头；返回是否真的翻过去了（翻不动=已经是第一/最后一页）。

    box 给的是「用哪一块画面判断页翻没翻」：普通列表用缩略图区，特别谜题用方块阵。
    """
    for _ in range(max(1, tries)):
        before = (_special_grid_gray(screen.grab_client()) if box is not None
                  else _list_thumb_gray(screen.grab_client()))
        screen.ensure_foreground()
        stop.check()
        screen.click_client(*pt)
        stop.sleep(0.95)
        after = (_special_grid_gray(screen.grab_client()) if box is not None
                 else _list_thumb_gray(screen.grab_client()))
        d = _thumb_diff(before, after)
        if d > LIST_FLIP_MIN_DIFF:
            log(f"  翻页成功（缩略图变化 {d * 100:.1f}%）")
            return True
    return False


# 本页扫描缓存：只用来判断「页有没有翻」。
# 不能按「图案指纹」缓存：光标停着的方块金框会变成青色高亮，图案区也有微变；
# 改用整页缩略图指纹做「翻页检测」——页没翻就复用本页结果，页翻了就整页重扫。
_PAGE_CACHE: Dict[str, object] = {"gray": None}
PAGE_CHANGE_MIN_DIFF = 0.02      # 同一页（含移动光标）约 0.1~1%，真正翻页 ≥3%

# 「这一关读不出来 → 先跳过去打别的关」用的跳过表（2026-09-29 深夜加）。
# 键是关卡方块在 1280×720 基准坐标下的位置。只在**重置也救不回来**时才加进去；
# 本页能打的都被跳过过一遍后会自动清空重来——所以读不通的关不会永远不打，只是不再
# 被连续撞。读数本身没修好之前，一个坏关卡不该把整轮拖死（真机 Lv3-016 就是这么卡住的）。
_AVOID_PTS: Set[Tuple[float, float]] = set()
# 每一格「读不通」栽了几次（键 = 关卡列表上那个点）。**必须跨轮记**：
# 第一轮失败会先重置本关再 continue（这一轮就结束了），下一轮 play_one 是全新的调用、
# read_fails 归零 —— 真机 2026-10-01 实测：同一格连续两轮都报「第 1 次」，
# 于是「第 2 次仍读不通就跳过」这条永远走不到，脚本原地反复重试同一格
# （用户看到的就是「它就是不去打别的方块」）。按落点记次数就能跨轮累计。
_READ_FAIL_BY_PT: Dict[Tuple[float, float], int] = {}
# 「下一轮选关时先绕开原本会选的那一格」。只在一种情况下用：
# 读不通的那一关**不是从列表点进来的**（脚本启动时人已经在关卡里、或上一轮重置后留在关卡里），
# 这时 `_LAST_PICKED_PT` 是 None，跳过表里没有它的位置 → 回列表后还是会点回同一格、原地死磕
# （真机 2026-10-01 实测：第 1 轮失败重置 → 第 2 轮还在这关 → 退出回列表 → 又点回同一格）。
# 用一次就清，最多让脚本多绕一格，不会漏打。
_AVOID_FIRST_PICK = False
_LAST_PICKED_PT: Optional[Tuple[float, float]] = None


def _reset_page_cache(gray: np.ndarray) -> None:
    _PAGE_CACHE["gray"] = gray


def _sync_page_cache(gray: np.ndarray) -> None:
    """页面内容变了（翻页 / 某一关新通关）就重置本页缓存。"""
    stored = _PAGE_CACHE.get("gray")
    if stored is None or _thumb_diff(stored, gray) > PAGE_CHANGE_MIN_DIFF:
        _reset_page_cache(gray)


def _special_page_task(screen: Screen, log=print):
    """在当前「特别谜题」页里找一个还没解开的格子（灰「?」挡板）。

    返回 ("play", 客户区坐标, 网格信息) / ("none", None, 网格信息) / ("not_list", None, None)。
    和普通列表一样带「跳过表」：读不通的方块先绕开，本页能打的都绕过一遍后清空重来。
    """
    global _LAST_PICKED_PT, _AVOID_FIRST_PICK
    img = screen.grab_client()
    if _list_kind(img) != LIST_MODE_SPECIAL:
        return "not_list", None, None
    _note_layout(LIST_MODE_SPECIAL, log)      # 变了就在面板日志里报一行
    info = detect_special_grid(img)
    if info is None:
        return "not_list", None, None
    _sync_page_cache(_special_grid_gray(img))
    w, h = img.size
    fallback: Optional[Tuple[float, float, Tuple[float, float]]] = None
    first_pick = True
    for (r, c, cx, cy) in info["plates"]:
        base = (round(cx * 1280.0 / w, 1), round(cy * 720.0 / h, 1))
        if _avoid_has(base):
            # 这一格之前读不出来：先绕开，本页别的都绕过一遍了才用它（见下）
            if fallback is None:
                fallback = (cx, cy, base)
            continue
        if first_pick and _AVOID_FIRST_PICK:
            # 上一关不是从列表点进来的（跳过表里没它的落点）→ 这一轮先绕开原本会选的这一格
            _AVOID_FIRST_PICK = False
            first_pick = False
            log("  上一关不是从列表点进来的，先绕开这一格（免得又点回同一格）。")
            if fallback is None:
                fallback = (cx, cy, base)
            continue
        _LAST_PICKED_PT = base
        return "play", (cx, cy), info
    if fallback is not None:
        log(f"  本页能打的方块都跳过过一遍了（跳过表 {len(_AVOID_PTS)} 关），清空重来。")
        _AVOID_PTS.clear()
        _READ_FAIL_BY_PT.clear()        # 跳过表清了，失败账也跟着清（每格重新算一次机会）
        _LAST_PICKED_PT = fallback[2]
        return "play", (fallback[0], fallback[1]), info
    return "none", None, info


def scan_special_list(screen: Screen, cfg: Config, stop: StopController, log=print,
                      max_pages: int = 12) -> Tuple[str, Optional[Tuple[float, float]]]:
    """在「特别谜题」的 5×5 方块阵里找下一个要打的关卡。

    只找**还没解开**的格子：没解开 = 灰色「?」挡板；解开过的格子显示的是那一小块画作像素
    （没有挡板）。本页没有就点右侧橙色「G」箭头翻页；翻不动（到最后一页，游戏会把箭头
    收起来）就当没有可打的了。
    返回：("play", 坐标) / ("all_done", None) / ("not_list", None)
    """
    status, pt, _info = _special_page_task(screen, log)
    if status in ("play", "not_list"):
        return status, pt
    for _ in range(max_pages):
        if not _flip_list_page(screen, stop, SPECIAL_PAGE_RIGHT_POINT, log,
                              box=SPECIAL_GRID_BOX):
            break
        status, pt, _info = _special_page_task(screen, log)
        if status in ("play", "not_list"):
            return status, pt
    return "all_done", None


def scan_level_list(screen: Screen, cfg: Config, stop: StopController, log=print,
                    max_pages: int = 12) -> Tuple[str, Optional[Tuple[float, float]], str]:
    """在关卡列表里找下一个要打的关卡（普通 / 特别谜题两种布局都从这里进）。

    普通谜题：只找**还没打过**的关卡（灰色「?」方块）；已通关的方块直接跳过。
    （2026-09-29：按用户要求删掉「已通关但没满三星就自动重打一遍」的星数模块——
      列表方块上不显示星星，真机上必须「点进关卡 → 菜单 → 退出」才能读到，又慢又容易
      把画面点乱，收益只是在已通关的关卡上刷满三星。）
    先扫当前页；本页没有就往后翻页；整个页签都没有就换页签（Lv1→Lv2→Lv3）。
    特别谜题：另一套布局，走 scan_special_list（见上）。
    返回：("play", 坐标, 布局) / ("all_done", None, 布局) / ("not_list", None, "")
    """
    head = screen.grab_client()
    kind = _list_kind(head)
    _note_layout(kind, log)
    if kind == LIST_MODE_SPECIAL:
        status, pt = scan_special_list(screen, cfg, stop, log, max_pages)
        return status, pt, LIST_MODE_SPECIAL

    def current_page_task():
        """在当前页找一个要打的关卡（跳过表里的先不看）。"""
        global _LAST_PICKED_PT, _AVOID_FIRST_PICK
        img = screen.grab_client()
        if not _is_level_list(img):
            return "not_list", None
        _sync_page_cache(_list_thumb_gray(img))
        w, h = img.size
        fallback: Optional[Tuple[float, float]] = None
        first_pick = True
        for cy in LIST_Y:
            for cx in LIST_X:
                pt = (cx * w / 1280.0, cy * h / 720.0)
                if _is_question_block(img, cx, cy):
                    if (cx, cy) in _AVOID_PTS:
                        # 这一关之前读不出来：先绕开，全都绕过一遍了才用它（见下）
                        if fallback is None:
                            fallback = pt
                        continue
                    if first_pick and _AVOID_FIRST_PICK:
                        # 上一关不是从列表点进来的 → 这一轮先绕开原本会选的这一关
                        # （见 `_AVOID_FIRST_PICK` 的注释；用一次就清）
                        _AVOID_FIRST_PICK = False
                        first_pick = False
                        log("  上一关不是从列表点进来的，先绕开这一关（免得又点回同一关）。")
                        if fallback is None:
                            fallback = pt
                        continue
                    _LAST_PICKED_PT = (cx, cy)
                    return "play", pt                     # 还没打过
        if fallback is not None:
            # 本页能打的关都被跳过过一遍了：清空跳过表重来。
            # 这样读不通的关不会"一次失败就永远不打"，但也不会被连续撞。
            log(f"  本页能打的关都跳过过一遍了（跳过表 {len(_AVOID_PTS)} 关），清空重来。")
            _AVOID_PTS.clear()
            _READ_FAIL_BY_PT.clear()    # 同上：失败账跟跳过表一起清
            return "play", fallback
        return "none", None

    status, pt = current_page_task()
    if status in ("play", "not_list"):
        return status, pt, (LIST_MODE_NORMAL if status == "play" else "")
    # 当前页没有要打的：往后翻页找
    for _ in range(max_pages):
        if not _flip_list_page(screen, stop, PAGE_RIGHT_POINT, log):
            break
        status, pt = current_page_task()
        if status in ("play", "not_list"):
            return status, pt, (LIST_MODE_NORMAL if status == "play" else "")
    # 本页签翻到底了：依次换到每个页签，从头再扫一遍
    for tab, tx, ty in TAB_POINTS:
        log(f"  本页签里没有要打的关卡了，换到 Lv{tab} 页签找…")
        screen.ensure_foreground()
        stop.check()
        screen.click_client(tx, ty)
        stop.sleep(1.1)
        status, pt = current_page_task()
        if status in ("play", "not_list"):
            return status, pt, (LIST_MODE_NORMAL if status == "play" else "")
        for _ in range(max_pages):
            if not _flip_list_page(screen, stop, PAGE_RIGHT_POINT, log):
                break
            status, pt = current_page_task()
            if status in ("play", "not_list"):
                return status, pt, (LIST_MODE_NORMAL if status == "play" else "")
    return "all_done", None, LIST_MODE_NORMAL


def _enter_special_level(screen: Screen, cfg: Config, stop: StopController,
                         pt: Tuple[float, float], log=print) -> bool:
    """点开一个「特别谜题」方块。

    特别谜题的选关是光标式的（底部写着「Z 选择 / 方向键 移动光标」）：第一下点击有可能
    只是把光标挪到那一格，需要再来一下才真的进去。所以点完等一下看画面变没变——
    变了（开始加载关卡）就绝不再点第二下，免得点进加载画面/棋盘上；没怎么变才补一下。
    """
    try:
        before = screen.grab_client()
        screen.ensure_foreground()
        stop.check()
        screen.click_client(*pt)
        stop.sleep(0.9)
        after = screen.grab_client()
        if _screen_changed_pct(before, after) > 0.08:
            return True
        if _list_kind(after) != LIST_MODE_SPECIAL:
            return True                     # 画面已经不是那一页方块阵了（进关了 / 在加载）
        log("  这一下像是只把光标挪过去了（画面几乎没变），再点一下确认…")
        stop.check()
        screen.click_client(*pt)
        stop.sleep(0.9)
        return True
    except BotAbort:
        raise
    except Exception as exc:
        log(f"  点开特别谜题方块失败：{exc}")
        return False


def _looks_dark(img: Image.Image) -> bool:
    """过关结算 / 弹窗会把整个画面压暗（实测正常棋盘 ≤0.20、结算 ≥0.28）。"""
    a = np.asarray(img.convert("L"), dtype=np.float32)
    return float((a < 110).mean()) > 0.24


def board_fill_ratio(img: Image.Image, geom: Geometry) -> float:
    """棋盘区域里「已上色」像素的占比（青色 (36,216,192) 是游戏的填色签名）。

    只回答一个问题：**眼前这个棋盘是不是已经被涂过东西了**。读数/自查失败时靠它区分两类：
      - 盘面干净（实测干净盘 0.00%）→ 这一关只是读不出来，跳过它是安全的；
      - 盘面有涂色（实测涂了 168 格时 34.47%）→ 跳过等于留下一盘没打完的棋，而且提示数字
        变灰之后更难恢复，**必须先清干净**。
    只认填色（青），不认灰叉/紫标记/高亮框：后两者可能是光标或游戏自己的美术混进来的。
    """
    x0 = max(0, int(geom.x1 - geom.cell_w * 0.4))
    x1 = min(img.width, int(geom.x2 + geom.cell_w * 0.4))
    y0 = max(0, int(geom.y1 - geom.cell_h * 0.4))
    y1 = min(img.height, int(geom.y2 + geom.cell_h * 0.4))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return 0.0
    a = np.asarray(img.crop((x0, y0, x1, y1)).convert("RGB"), dtype=np.int16)
    if a.size == 0:
        return 0.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    fill = (np.abs(r - 36) < 46) & (np.abs(g - 216) < 46) & (np.abs(b - 192) < 46)
    return float(fill.mean())


FILL_DIRTY_RATIO = 0.02          # 超过这个占比就认为「盘面已经被涂过」（干净盘 0.00%）


def reset_level(screen: Screen, cfg: Config, stop: StopController, log=print,
                force: bool = False) -> bool:
    """真实鼠标「菜单 → 重置」：把涂过一半（提示数字变灰）的关卡恢复成干净状态。

    force=True 时跳过"眼前是不是棋盘"这层守卫——只在调用方**自己数过棋盘像素、确认盘面
    确实有涂色**时用（那种情况下棋盘一定在眼前，守卫反而会挡住该做的清理）。菜单点开后
    "画面确实变了"这层校验仍然保留，所以即便 force 也不会乱点。
    """
    try:
        screen.ensure_foreground()
        stop.check()
        before = screen.grab_client()
        # 先确认眼前是棋盘再点菜单：菜单按钮的坐标（按 1280×720 标定后按比例换算）在关卡列表
        # 这类界面上会落到别的东西上，盲点等于误点。
        why_ui = None if force else _probably_not_board(before)
        if why_ui:
            log(f"  眼前是「{why_ui}」而不是棋盘，先不点菜单/重置（避免误点）。")
            return False
        mx, my = _ui_point(screen, 1230.0, 25.0)
        log("  打开游戏菜单…")
        screen.click_client(mx, my)
        stop.sleep(0.7)
        if _screen_changed_pct(before, screen.grab_client()) < 0.08:
            log("  菜单好像没打开（画面几乎没变化），先不点重置。")
            return False
        rx, ry = _ui_point(screen, 640.0, 364.0)
        log("  点「重置」重开本关…")
        screen.click_client(rx, ry)
        stop.sleep(1.2)
        return True
    except BotAbort:
        raise
    except Exception as exc:
        log(f"  自动重置失败：{exc}")
        return False


def _frame_has_board(img: Image.Image) -> bool:
    """这一帧里检不检出棋盘？（不看它跟我们配置里的几何一不一样）

    `_board_line_visible` 要求「跟配置里那套几何**一样**」，而「关卡完成」那一屏的棋盘会
    被结算演出挪位置/缩放，于是 `_same_board` 不成立、`_board_line_visible` 给 False ——
    所以要判「棋盘还在眼前」得用这个更宽的版本（真机 2026-10-01：「关卡完成」那一屏
    `auto_detect_candidates` 有 1 个候选，`_board_line_visible(img, cfg.geom)` 却是 False）。
    """
    try:
        return bool(auto_detect_candidates(img))
    except BotAbort:
        raise
    except Exception:
        return False


def leave_level_to_list(screen: Screen, cfg: Config, stop: StopController, log=print,
                        from_clear: bool = False) -> bool:
    """「菜单 → 退出」回到关卡列表（读不通的关卡要跳过去时用）。

    和 reset_level 一样，先确认眼前真是棋盘再点菜单：菜单/退出这两个坐标在关卡列表这类
    界面上会落到别的东西上，盲点等于误点。点完再确认真的回到了列表；没回去就返回 False，
    让调用方退回"等画面变化"的老路子——绝不硬来。

    `from_clear=True`：这次不是「读不通要跳过」，而是**刚把这一关打通**、正卡在过关界面上
    （「关卡完成」那一屏）。那一屏会被 `_is_result_screen` 认成「结算/画作界面」，
    于是这道"先确认是棋盘"的闸门会把它挡下来 —— 但它右边明明写着「Esc / 菜单」，
    菜单/退出这两个坐标在这一屏上就是有效按钮（真机 2026-10-01 实测：这一屏点「继续」
    连点 20 次纹丝不动，走「菜单 → 退出」一次就回到了方块阵），所以这时允许直接点，
    点完照旧要确认「真的回到列表了」才算数。
    """
    try:
        screen.ensure_foreground()
        stop.check()
        before = screen.grab_client()
        if not from_clear:
            why_ui = _probably_not_board(before)
            if why_ui:
                log(f"  眼前是「{why_ui}」而不是棋盘，先不点菜单/退出（避免误点）。")
                return False
        mx, my = _ui_point(screen, 1230.0, 25.0)
        screen.click_client(mx, my)
        stop.sleep(0.7)
        if _screen_changed_pct(before, screen.grab_client()) < 0.08:
            log("  菜单好像没打开（画面几乎没变化），先不点退出。")
            return False
        rx, ry = _ui_point(screen, 640.0, 231.0)
        log("  点「退出」回到关卡列表…")
        screen.click_client(rx, ry)
        stop.sleep(1.2)
        if _is_level_list(screen.grab_client()):
            return True
        screen.click_client(rx, ry)          # 菜单还开着（没点中退出）→ 再点一次
        stop.sleep(1.0)
        return bool(_is_level_list(screen.grab_client()))
    except BotAbort:
        raise
    except Exception as exc:
        log(f"  退出本关失败：{exc}")
        return False


def _board_ready(screen: Screen, cfg: Config, img: Image.Image, log=print) -> bool:
    """眼前有没有「能直接开工」的棋盘（换了棋盘尺寸也能认出来）。

    只做网格识别（不做整帧 OCR），很快；识别到新尺寸时才读一帧校验并更新配置。
    """
    cands: List[Geometry] = []
    try:
        cands = auto_detect_candidates(img)
    except BotAbort:
        raise
    except Exception:
        cands = []
    if cfg.geom is not None and any(_same_board(c, cfg.geom) for c in cands):
        return True
    # 网格和配置对不上：多半是换了难度页签（5×5 ↔ 15×15 ↔ 20×20），
    # 先确认候选确实是棋盘，再走一次「读一帧校验」来换几何
    for c in cands[:2]:
        try:
            if not board_present(screen, c)[0]:
                continue
        except BotAbort:
            raise
        except Exception:
            continue
        return resolve_geometry(screen, cfg, log, attempts=2) is not None
    if cfg.geom is not None:
        try:
            return board_present(screen, cfg.geom)[0]
        except BotAbort:
            raise
        except Exception:
            return False
    return False


def advance_after_clear(screen: Screen, cfg: Config, stop: StopController,
                        log=print, timeout: float = 30.0) -> bool:
    """填完最后一格后，把过关动画/结算界面/解锁弹窗一路点掉。

    每 0.3s 判断一次画面类型，是弹窗就点「返回」、是结算就点「继续」，
    直到回到关卡列表。返回 True 表示已经走完（可以继续找下一关）。

    2026-10-01（特别谜题真机）：过关后的那个界面**不一定认「画面中央」这一下**——
    面板那一轮日志里「过关结算：点一下继续…」连点了 22 次都没回到方块阵（用户看到的是
    「一排完了就不换行了」）。现在：同一点连点几次画面**一动不动**就换下一个候选落点，
    并且把当时的画面存下来（`debug/clear_*_result_*.png`），下次能直接看这一屏长什么样。
    ⚠ 只在「眼前没有棋盘」时才换备用落点：棋盘还亮着时乱点就是把格子涂花。
    """
    end = time.time() + timeout
    clicks = 0
    unknown = 0
    board_note = [False]            # 「兜底不往棋盘上点」这句只提示一次
    # 「继续」候选落点（1280×720 基准）：先按老位置点，点了没反应再换。
    candidates = [_CONTINUE_POINT, (640.0, 650.0), (640.0, 250.0)]
    ci = 0
    idle = 0                        # 同一个落点连点几次画面都不动
    dumps = 0
    menu_tried = False              # 「关卡完成」那一屏只提示一次「改走菜单」
    while time.time() < end:
        stop.check()
        try:
            screen.ensure_foreground()
            img = screen.grab_client()
        except BotAbort:
            raise
        except Exception:
            stop.sleep(0.3)
            continue
        if _has_orange_button(img):
            dismiss_unlock_popup(screen, cfg, stop, log)
            clicks += 1
            unknown = 0
            continue
        if _is_level_list(img):
            return True                     # 到关卡列表了
        if _is_result_screen(img) or _looks_dark(img):
            has_board = _frame_has_board(img)
            if clicks == 0 or (idle and idle % 3 == 0 and dumps < 4):
                # 把「点不动」的这一屏存下来，方便事后看它到底是什么界面
                # （文件名带上「棋盘还在不在」，一眼能分清是「关卡完成」那一屏还是别的界面）
                # ⚠ 假屏幕（--selftest 的端到端用例）不落盘：那是测试自己造的画面，存下来只是垃圾
                try:
                    if not getattr(screen, "is_fake", False):
                        DEBUG_DIR.mkdir(exist_ok=True)
                        tag = "board" if has_board else "screen"
                        shot = DEBUG_DIR / ("clear_%s_%s%d.png"
                                            % (time.strftime("%Y%m%d_%H%M%S"), tag, dumps + 1))
                        img.save(shot)
                        dumps += 1
                        log(f"  [存档] 过关后的界面已存到 {shot.name}（点了 {clicks} 次没走掉）")
                except Exception:
                    pass
            if idle >= 3 and has_board:
                # 棋盘还在眼前 + 点了好几下画面一动不动 → 这是「关卡完成」那一屏（棋盘填满、
                # 「关卡完成」四个大字压在棋盘上、右边写着「Esc / 菜单」）。它**不吃「点一下继续」**：
                # 真机 2026-10-01 实测在这一屏上连点 20 次画面纹丝不动。
                # 正确走法是「菜单 → 退出」回列表（同一晚读不通的那一关就是靠这条路回去的）。
                if not menu_tried:
                    menu_tried = True
                    log("  点了好几下没反应、棋盘还亮着（多半是「关卡完成」那一屏）：改走「菜单 → 退出」。")
                if leave_level_to_list(screen, cfg, stop, log, from_clear=True):
                    return True
                idle = 0
                stop.sleep(1.0)
                continue
            if idle >= 3:
                ci = (ci + 1) % len(candidates)
                idle = 0
                log(f"  这个界面点「继续」没反应，换第 {ci + 1} 个候选落点试试…")
            if clicks < 2 or (idle and idle % 3 == 0):
                log("  过关结算：点一下继续…")
            px, py = _ui_point(screen, *candidates[ci])
            screen.ensure_foreground()
            stop.check()
            screen.click_client(px, py)
            clicks += 1
            unknown = 0
            stop.sleep(0.85)
            try:
                if _screen_changed_pct(img, screen.grab_client()) < 0.02:
                    idle += 1
                else:
                    idle = 0
            except Exception:
                idle = 0
            continue
        # 还亮着的棋盘 = 过关动画播放中，等一下。
        # 兜底点一下只对「眼前确实不是棋盘」的界面做，而且落点要挪到棋盘外的空档：
        # 1280×720 时代当画面中央用的 (640,480)，按比例换算到 1920×1080 正好落在棋盘正中间
        # ——盲点只要在棋盘还亮着的时候触发一次，就会把那一格涂花（这就是「误点」）。
        unknown += 1
        on_board = _board_line_visible(img, cfg.geom)
        if unknown >= 12 and clicks < 8 and not on_board:
            log("  这个界面认不出来，点一下棋盘外的空档试试…")
            screen.ensure_foreground()
            stop.check()
            px, py = _safe_ui_point(screen, cfg.geom, *_CONTINUE_POINT)
            screen.click_client(px, py)
            clicks += 1
            unknown = 0
            stop.sleep(0.8)
            continue
        if unknown >= 12:
            unknown = 0
            if on_board and not board_note[0]:
                board_note[0] = True
                log("  眼前还是棋盘（过关动画没走完，或者这关没判过）：兜底不往棋盘上点，继续等。")
        stop.sleep(0.3)
    return False


def acquire_board(screen: Screen, cfg: Config, stop: StopController,
                  timeout: float, log=print) -> Optional[Geometry]:
    """
    弄到一个可以开工的棋盘。每 ~0.5s 看一帧，按固定优先级处理：
      解锁弹窗 -> 关卡列表(找下一个未完成关卡，自动翻页/换页签) -> 眼前的棋盘 -> 结算界面
    全程用真实鼠标点击（这个游戏不认脚本合成的按键）。
    """
    end = time.time() + timeout
    warned = False
    tries = 0
    center_clicks = 0
    last_msg = 0.0
    last_pick = (0.0, 0.0, 0.0)          # 刚点过的关卡方块（时间, x, y）
    while time.time() < end:
        stop.check()
        try:
            screen.ensure_foreground()
            img = screen.grab_client()
        except BotAbort:
            raise
        except Exception:
            stop.sleep(0.4)
            continue
        tries += 1
        # 1) 过关解锁弹窗：点橙色「返回」关掉
        if _has_orange_button(img):
            if dismiss_unlock_popup(screen, cfg, stop, log):
                tries = 0
                continue
        # 2) 关卡列表：找下一个要打的关卡（普通谜题 / 特别谜题两种布局；自动翻页、换页签）
        if _is_level_list(img):
            status, pt, kind = scan_level_list(screen, cfg, stop, log)
            if status == "play":
                px, py = pt
                if (time.time() - last_pick[0] < 5.0
                        and abs(px - last_pick[1]) < 6 and abs(py - last_pick[2]) < 6):
                    # 同一关刚刚点过（进关加载要 2s 左右，画面还是列表的样子），
                    # 别再点一次，等它切过去
                    stop.sleep(0.8)
                    continue
                if kind == LIST_MODE_SPECIAL:
                    log("  已回到关卡列表（特别谜题方块阵），点开这一格…")
                    screen.ensure_foreground()
                    stop.check()
                    _enter_special_level(screen, cfg, stop, pt, log)
                else:
                    log("  已回到关卡列表，点开这一关…")
                    screen.ensure_foreground()
                    stop.check()
                    screen.click_client(*pt)
                last_pick = (time.time(), px, py)
                stop.sleep(1.6)
                tries = 0
                continue
            if status == "all_done":
                raise BotAbort("没有没打过的关卡了（各页签里的关卡都已经通关）")
            # not_list：画面又跳走了，继续循环
        # 3) 眼前就有棋盘：绝不点中央（那会点到棋盘上把格子涂花）
        if not _looks_dark(img) and _board_ready(screen, cfg, img, log):
            geom = cfg.geom
            if geom is not None:
                return geom
        # 4) 结算/弹窗界面：点一下就能继续。
        #    认不出的界面也兜底点一下，但**眼前还看得见棋盘时绝不点**——兜底点按比例换算
        #    正好落在棋盘正中（1280×720 的 (640,480) → 1920×1080 的 (960,720)），在棋盘上
        #    点一下就是把那一格涂花；兜底点在棋盘外时也挪到棋盘外的空档，次数上限照旧。
        on_board = _board_line_visible(img, cfg.geom)
        known_ui = _looks_dark(img) or _is_result_screen(img)
        if center_clicks < 8 and (known_ui or (tries >= 4 and not on_board)):
            if known_ui:
                log("  像是过关结算/其它界面，点一下继续…")
                px, py = _ui_point(screen, *_CONTINUE_POINT)
            else:
                log("  界面认不出来，点一下棋盘外的空档试试…")
                px, py = _safe_ui_point(screen, cfg.geom, *_CONTINUE_POINT)
            screen.ensure_foreground()
            stop.check()
            screen.click_client(px, py)
            center_clicks += 1
            stop.sleep(1.0)
            tries = 0
            continue
        if on_board and not warned:
            warned = True
            log("  画面里有棋盘，但还没认出可开工的尺寸（可能在进关动画里）：不往棋盘上点，继续看。")
        if not warned:
            warned = True
            log("  还没出现棋盘（可能停在结算框、关卡列表或主菜单）。")
            log("  请手动进一关，脚本会继续等；按 F8 / Esc 可随时停止。")
        # 卡住时要能在日志里看出来（每 20s 报一次自己看到的画面类型）
        if time.time() - last_msg > 20.0:
            last_msg = time.time()
            kind = ("关卡列表" if _is_level_list(img) else
                    "暗色界面" if _looks_dark(img) else
                    "结算/画作" if _is_result_screen(img) else "未知界面")
            log(f"  还在等可开工的画面：现在像「{kind}」，画面已看 {tries} 次…")
        stop.sleep(0.8)
    # 卡住时把现场画面存下来，方便事后定位（不依赖任何具体关卡）
    if not warned:
        return None
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        shot = DEBUG_DIR / f"stuck_{time.strftime('%Y%m%d_%H%M%S')}.png"
        screen.grab_client().save(shot)
        log(f"  已保存卡住时的画面：{shot}")
    except Exception:
        pass
    return None


STUCK_LIMIT = 12                     # 连续这么多轮没能真正过掉一关，就判定卡死
# （2026-09-29 23:2x：从 6 抬到 12。现在"读不通/读错了的关"会先跳过去打别的关，只有一关都
#  打不通才该中止；而一关坏掉可能贡献 2 次失败计数，6 太容易被几个坏关卡堆到。）

def run_loop(screen: Screen, geom: Geometry, cfg: Config, stop: StopController,
             rounds: int, confirm: bool, log=print) -> None:
    """懒人循环：自动等棋盘 → 识别 → 求解 → 填格 → 过关 → 继续，涂过的关卡自动重置。"""
    global _AVOID_FIRST_PICK

    def stuck_abort() -> BotAbort:
        return BotAbort(
            f"连续 {STUCK_LIMIT} 轮都没能真正过掉这一关（一直在「重置本关 → 重新填 → 却进不了"
            "下一关」之间打转）。最常见的原因是这一关的提示数字被读错了一两个，导致自动填出来"
            "的答案是错的、游戏判定没过关；也可能是过关界面没被认出来。脚本再跑下去也过不了，"
            "已中止以免空转。建议：看一眼这关自动填的结果哪里和答案不符，或用 --dry-run 核对"
            "提示数字，也可手动换一关后重新启动。")

    total_cells = 0
    ok_rounds = 0
    fails = 0
    dup_streak = 0
    read_fails = 0
    auto_fixes = 0
    stuck = 0                        # 连续「没有真正过掉一关」的轮数（防原地死循环）
    prev_key = None
    last_pt = None                   # 上一轮挑的关卡位置（换了关就把识别失败计数清零）
    i = 0
    t_start = time.time()
    while rounds <= 0 or i < rounds:
        i += 1
        # 换了一关：识别失败计数从头算，别把上一关的失败次数算到这一关头上
        if _LAST_PICKED_PT != last_pt:
            last_pt = _LAST_PICKED_PT
            read_fails = 0
        log(f"===== 第 {i} 关 =====" if rounds <= 0 else f"===== 第 {i}/{rounds} 关 =====")
        # 拿到棋盘：自动等 / 关弹窗 / 列表选下一关 / 结算点继续（反复重试，不会干等）
        geom = acquire_board(screen, cfg, stop, 600.0, log)
        if geom is None:
            raise BotAbort("等了 10 分钟仍没有棋盘，已中止")
        try:
            result = play_one(screen, geom, cfg, stop, confirm=confirm, dry_run=False,
                              log=log, prev_key=prev_key)
        except BotAbort as exc:
            # 识别失败通常有两种原因：画面还在结算框/换关动画（重试即可）；
            # 或者这关已经被涂过一部分——游戏会把提示数字变灰，OCR 读不准（自动重置）
            read_fails = _note_read_fail(_LAST_PICKED_PT, read_fails)
            stuck += 1
            log(f"  本关识别失败（第 {read_fails} 次）：{exc}")
            if stuck >= STUCK_LIMIT:
                raise stuck_abort()
            # ★ 关键分流（2026-09-29 深夜补）：先看盘面有没有涂色，再决定"能不能跳"。
            #   读不出来 + 盘面干净 → 跳过是安全的（这一关什么都没动过）。
            #   读不出来/自查没过 + 盘面有涂色 → **绝不能跳**：跳过去就留下一盘没打完的棋，
            #   而且提示数字变灰后更难恢复（真机 Lv3-019 就是这么被"涂一半跳到下一关"的）。
            try:
                ratio = board_fill_ratio(screen.grab_client(), geom)
            except Exception:
                ratio = 0.0
            if ratio > FILL_DIRTY_RATIO:
                log(f"  本关盘面已经有涂色（约 {ratio * 100:.0f}%），不能放着不管，先清干净…")
                ok = False
                for _try in range(3):
                    if reset_level(screen, cfg, stop, log, force=True):
                        ok = True
                        break
                    stop.sleep(1.5)
                if not ok:
                    raise BotAbort(
                        f"本关已经涂了一半（约 {ratio * 100:.0f}%）但自动重置不了。为了不留下一盘没打完"
                        "的棋，脚本先停在这里——请手动「菜单 → 重置」把它清干净，或换一关再启动。")
                log("  已自动重置本关，盘面清干净了。")
                # ★ 清干净之后**也走跳过**（2026-09-29 23:2x 修正）：
                #   盘面脏只说明"上一次没打完"，不等于"这一关读得出来"。真机 25×25 那关就是
                #   读数本身错 → 涂到一半自查不过 → 清干净 → 重读照旧错 → 再涂再失败，
                #   原地空转 6 轮才被 stuck 上限中止。清干净只是不留脏棋，救不了错读数。
                #   盘面已经干净，跳过不会留下没打完的棋；跳过表清空重来后它还会被再试一次。
                if _LAST_PICKED_PT is not None:
                    _AVOID_PTS.add(_LAST_PICKED_PT)
                    log(f"  这一关的读数不可信（涂了又自查不过），先跳过去打别的关；"
                        f"跳过表现有 {len(_AVOID_PTS)} 关。")
                read_fails = 0
                auto_fixes = 0
                if leave_level_to_list(screen, cfg, stop, log):
                    log("  已回到关卡列表，继续找下一关。")
                    stop.sleep(1.0)
                    continue
                stop.sleep(1.5)
                continue
            if read_fails == 1:
                # 第一次失败（且盘面干净）：先重置一次，清掉"提示数字被涂灰"这类脏状态；
                # 重置不适用（眼前不是棋盘）就干脆直接再识别一次。
                if reset_level(screen, cfg, stop, log):
                    log("  已自动重置本关，再识别一次…")
                    auto_fixes += 1
                else:
                    log("  先重置不了，直接再识别一次（几何由 acquire_board 重挂）…")
                stop.sleep(1.5)
                continue
            # 第二次还读不通 → **别把整轮卡死在这一关上**（2026-09-29 深夜改）。
            # 退出本关回列表，并把这关记进跳过表，先去打别的关；本页能打的都被跳过过一遍后
            # 跳过表会自动清空重来（见 scan_level_list）。只有"一关都打不通"时 stuck 才会
            # 攒到 STUCK_LIMIT，那时才真中止——这才是"读不通的关"应有的行为。
            log("  这关读不通（多半是提示数字被读错，或它已经被涂过一部分、提示变灰了）。")
            if _LAST_PICKED_PT is not None:
                _AVOID_PTS.add(_LAST_PICKED_PT)
                log(f"  这关先跳过（记入跳过表，回列表打别的关）；跳过表现有 {len(_AVOID_PTS)} 关。")
            else:
                # 这一关不是从列表点进来的（跳过表里没它的位置）→ 让下一轮选关绕开原本会选的那一格
                _AVOID_FIRST_PICK = True
                log("  这关不是从列表点进来的（跳过表里没它的位置）：回列表后先绕开原本要选的那一格。")
            if leave_level_to_list(screen, cfg, stop, log):
                log("  已回到关卡列表，继续找下一关。")
                read_fails = 0
                auto_fixes = 0
                stop.sleep(1.0)
                continue
            log("  退不回列表，改等画面自己变（在游戏里手动换一关也行）；按 F8 / Esc 可停止。")
            sig = board_signature(screen, geom)
            if not wait_until_changed(screen, geom, cfg, stop, sig, 300.0, log):
                raise BotAbort("等了 5 分钟画面也没变，已中止")
            log("  画面变了，重新识别棋盘尺寸…")
            geom = resolve_geometry(screen, cfg, log) or geom
            read_fails = 0
            auto_fixes = 0
            # 注意：这里不清 stuck。画面变化可能只是动画/自己打叉引起的噪声，
            # 不等于「真过了一关」；只有 advance_after_clear 成功才算进展。
            continue
        read_fails = 0
        _forget_read_fail(_LAST_PICKED_PT)      # 这一格读通了，清掉它的失败账
        if result.get("duplicate") or result.get("prepainted") or result.get("dirty"):
            dup_streak += 1
            stuck += 1
            if result.get("prepainted"):
                log("  本关已经被手工涂过一部分。")
            if result.get("dirty"):
                log("  本关已经被涂过 / 打过叉，先重置成干净状态。")
            if stuck >= STUCK_LIMIT:
                raise stuck_abort()
            if auto_fixes < 2 and reset_level(screen, cfg, stop, log):
                log("  已自动重置本关，重新开打。")
                auto_fixes += 1
                dup_streak = 0
                continue
            if dup_streak >= 2:
                log("  连续几次都没有可下手的棋盘（多半都已经被涂过一部分）。")
                log("  请手动换一关（菜单 → 退出 → 点下一个方块）或用「菜单 → 重置」重开本关，")
                log("  脚本会在原地等着；按 F8 / Esc 可随时停止。")
                sig = board_signature(screen, geom)
                if not wait_until_changed(screen, geom, cfg, stop, sig, 300.0, log):
                    raise BotAbort("等了 5 分钟画面也没变，已中止")
                geom = resolve_geometry(screen, cfg, log) or geom
                dup_streak = 0
                auto_fixes = 0
                # 同上：不清 stuck，避免「画面一变就清零」导致无限打转。
                continue
            _click_next_button(screen, cfg, stop, log)
            if not cfg.next_button:
                stop.sleep(2.0)
            continue
        dup_streak = 0
        prev_key = result.get("key")
        if result["bad"] > 0:
            fails += 1
        else:
            ok_rounds += 1
            total_cells += result["filled"]
        if fails >= 3:
            raise BotAbort("连续 3 关填格结果异常，已中止（请检查游戏窗口是否被遮挡/缩放变化）")
        # 过关动画 → 结算界面 →（可能的解锁弹窗）→ 关卡列表：
        # 一路点掉，不等满超时，也不空转
        if advance_after_clear(screen, cfg, stop, log, timeout=cfg.clear_wait_timeout):
            # 真的过掉一关 → 清掉「无进展」和自动重置计数，进入正常下一关
            stuck = 0
            auto_fixes = 0
        else:
            # 填完了却没进下一关：多半这关没被判定过关，别把 auto_fixes 清零
            # （否则会无限「重置 → 重填 → 过不了」空转）
            stuck += 1
            log("  过关流程好像卡住了（画面一直没变化），下一关再试…")
        if stuck >= STUCK_LIMIT:
            raise stuck_abort()
    log(f"循环结束：成功 {ok_rounds} 关，异常 {fails} 关，共填 {total_cells} 格，"
        f"总用时 {(time.time() - t_start) / 60:.1f} 分钟")


# --------------------------------------------------------------------------
# 校准
# --------------------------------------------------------------------------


def _prompt(msg: str, default: str = "") -> str:
    try:
        txt = input(msg).strip()
    except Exception:
        return default
    return txt or default


def _validate_geometry(screen: Screen, geom: Geometry, cfg: Config, log) -> Optional[Puzzle]:
    """用「读一遍提示数字」来验证一套几何参数是否靠谱。"""
    cfg.geom = geom
    try:
        return read_puzzle(screen, geom, cfg, log)
    except BotAbort as exc:
        log(f"  校验未通过：{exc}")
        return None


def calibrate(path: Path, keywords: Sequence[str], log=print, manual: bool = False) -> Config:
    cfg = Config.load(path)
    cfg.window_keywords = list(keywords)
    hwnd = find_game_window(cfg.window_keywords)
    if hwnd is None:
        raise BotAbort("没有找到游戏窗口，请先启动《Hatsune Miku Logic Paint S》并停在一关的棋盘界面")
    screen = Screen(hwnd)
    ensure_foreground(hwnd)
    stop = StopController(cfg)
    log(f"已找到窗口：{_window_title(hwnd)}")
    log("")
    geom: Optional[Geometry] = None

    if not manual:
        log("【自动定位】正在识别棋盘网格…")
        img = screen.grab_client()
        cands = auto_detect_candidates(img)
        if not cands:
            log("  自动定位失败，改用鼠标校准（F8）")
        for idx, auto in enumerate(cands, 1):
            log(f"  候选 {idx}：{auto.rows}×{auto.cols}，格宽 {auto.cell_w:.2f}px、"
                f"格高 {auto.cell_h:.2f}px（左上格中心 {auto.x1:.0f},{auto.y1:.0f}），正在校验…")
            if _validate_geometry(screen, auto, cfg, log) is not None:
                geom = auto
                break
        if geom is None:
            log("  所有候选都没通过校验，改用鼠标校准（F8）")

    if geom is None:
        log("【手动校准】把鼠标移到「棋盘左上角第一个格子」的中心，然后按 F8")
        if stop.wait_key([VK_F8], timeout=600) is None:
            raise BotAbort("等待 F8 超时")
        sx1, sy1 = cursor_pos()
        ox, oy = client_origin(hwnd)
        x1, y1 = sx1 - ox, sy1 - oy
        log(f"  记录：客户区坐标 ({x1}, {y1})")
        log("【手动校准】把鼠标移到「棋盘右下角最后一个格子」的中心，然后按 F8")
        if stop.wait_key([VK_F8], timeout=600) is None:
            raise BotAbort("等待 F8 超时")
        sx2, sy2 = cursor_pos()
        ox, oy = client_origin(hwnd)
        x2, y2 = sx2 - ox, sy2 - oy
        log(f"  记录：客户区坐标 ({x2}, {y2})")
        if x2 - x1 < 40 or y2 - y1 < 40:
            raise BotAbort("两次记录的位置太近，请重新校准")
        img = screen.grab_client()
        det_x = detect_size_from_clicks(img, x1, x2, "x")
        det_y = detect_size_from_clicks(img, y1, y2, "y")
        if det_x is None or det_y is None:
            raise BotAbort("自动推断尺寸失败，请确认游戏停在棋盘界面")
        cols, px, x1c, _ = det_x
        rows, py, y1c, _ = det_y
        cols, rows = snap_size(cols), snap_size(rows)
        log(f"  检测结果：{rows}×{cols} 格，格宽 {px:.2f}px、格高 {py:.2f}px")
        geom = Geometry(x1=x1c, y1=y1c, x2=x1c + (cols - 1) * px, y2=y1c + (rows - 1) * py,
                        cols=cols, rows=rows)
        ans = _prompt(f"  Enter 确认 {rows}×{cols}，或输入「15」「15x20」手动指定： ")
        if ans:
            m = re.match(r"^\s*(\d+)\s*(?:[xX×*,]\s*(\d+))?\s*$", ans)
            if m:
                r2 = int(m.group(1))
                c2 = int(m.group(2)) if m.group(2) else r2
                px = (geom.x2 - geom.x1) / max(1, c2 - 1)
                py = (geom.y2 - geom.y1) / max(1, r2 - 1)
                geom = Geometry(x1=geom.x1, y1=geom.y1, x2=geom.x1 + (c2 - 1) * px,
                                y2=geom.y1 + (r2 - 1) * py, cols=c2, rows=r2)
        puzzle = _validate_geometry(screen, geom, cfg, log)
        if puzzle is None:
            log("  [警告] 校验未通过；可重新运行 --calibrate 并手动输入尺寸")

    cfg.geom = geom
    log("")
    log("【可选】把鼠标移到「过关后要点击的按钮」（例如下一关/继续）上按 F8 记录，按 F7 跳过")
    vk = stop.wait_key([VK_F8, VK_F7], timeout=60)
    if vk == VK_F8:
        bx, by = cursor_pos()
        ox, oy = client_origin(hwnd)
        cfg.next_button = [float(bx - ox), float(by - oy)]
        log(f"  已记录按钮位置：({cfg.next_button[0]:.0f}, {cfg.next_button[1]:.0f})")
    else:
        cfg.next_button = None
        log("  跳过（循环模式会等你手动进入下一关）")
    cfg.save(path)
    log("")
    log(f"校准完成，配置已保存：{path}")
    return cfg


# --------------------------------------------------------------------------
# 诊断 / 自检
# --------------------------------------------------------------------------


def probe(screen: Screen, geom: Geometry, cfg: Config, log=print) -> None:
    log("> 画面诊断")
    present, detail = board_present(screen, geom)
    log(f"  棋盘判定：{'是' if present else '否'}（{detail}）")
    img = screen.grab_client()
    auto = (auto_detect_candidates(img) or [None])[0]
    if auto is not None:
        log(f"  自动定位：{auto.rows}×{auto.cols}，格宽 {auto.cell_w:.2f}px，"
            f"左上格中心 ({auto.x1:.0f},{auto.y1:.0f})")
    else:
        log("  自动定位：失败")
    boxes: List[tuple] = []
    rows_map, bad_r = _read_clue_axis(img, geom, "R", cfg.ocr_workers, boxes)
    cols_map, bad_c = _read_clue_axis(img, geom, "C", cfg.ocr_workers, boxes)
    rows_show = [[c[0] if c else None for c in rows_map.get(r, [])] for r in range(geom.rows)]
    cols_show = [[c[0] if c else None for c in cols_map.get(c, [])] for c in range(geom.cols)]
    log(f"  直接读数：行提示 {rows_show}")
    log(f"           列提示 {cols_show}")
    if bad_r or bad_c:
        log(f"  识别失败的提示块：{len(bad_r) + len(bad_c)} 个")
    try:
        puzzle = read_puzzle(screen, geom, cfg, log)
        log(f"  识别：{puzzle.rows_n}×{puzzle.cols_n}，行和={sum(puzzle.row_sums)}，列和={sum(puzzle.col_sums)}")
        grid = PuzzleSolver(puzzle.rows, puzzle.cols).solve(cfg.solver_time_limit)
        if grid:
            print(format_solution(grid, puzzle.rows))
    except BotAbort as exc:
        log(f"  识别失败：{exc}")
    DEBUG_DIR.mkdir(exist_ok=True)
    out = DEBUG_DIR / f"probe_{time.strftime('%Y%m%d_%H%M%S')}.png"
    draw = ImageDraw.Draw(img)
    for r in range(geom.rows):
        for c in range(geom.cols):
            cx, cy = geom.cell_center(r, c)
            hw, hh = geom.cell_w / 2, geom.cell_h / 2
            draw.rectangle([cx - hw, cy - hh, cx + hw, cy + hh], outline=(0, 255, 0))
    for b in boxes:
        draw.rectangle(list(b), outline=(255, 80, 200), width=2)
    bx0 = geom.x1 - geom.cell_w / 2 - geom.max_row_slots * geom.cell_w * 1.1
    by0 = geom.y1 - geom.cell_h / 2 - geom.max_col_slots * geom.cell_h * 1.1
    draw.rectangle([max(0, bx0), max(0, by0),
                    geom.x2 + geom.cell_w / 2, geom.y2 + geom.cell_h / 2], outline=(255, 220, 0))
    img.save(out)
    log(f"  已保存标注截图：{out}（绿=棋盘格，粉=识别到的数字，黄=搜索范围）")


def _find_test_font(size: int) -> ImageFont.FreeTypeFont:
    for p in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf",
              r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\msyhbd.ttc"):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()


class FakeScreen:
    """自检用：把渲染出来的合成画面当游戏画面，点击会真的把格子涂色。"""

    is_fake = True      # 假屏幕不模拟「游戏把已满足的提示变灰」，所以 play_one 会跳过那道自查

    def __init__(self, img: Image.Image, geom: Geometry, empty_color, paint_color):
        self.img = img
        self.geom = geom
        self.empty_color = empty_color
        self.paint_color = paint_color

    def alive(self) -> bool:
        return True

    def ensure_foreground(self) -> None:
        pass

    def grab_client(self) -> Image.Image:
        return self.img.copy()

    def click_client(self, x: float, y: float, hold: float = 0.016,
                     right: bool = False) -> None:
        c = int(round((x - self.geom.x1) / self.geom.cell_w))
        r = int(round((y - self.geom.y1) / self.geom.cell_h))
        if not (0 <= r < self.geom.rows and 0 <= c < self.geom.cols):
            return
        cx, cy = self.geom.cell_center(r, c)
        if abs(cx - x) > self.geom.cell_w / 2 or abs(cy - y) > self.geom.cell_h / 2:
            return
        draw = ImageDraw.Draw(self.img)
        if right:  # 右键：画一个浅灰的叉（与填色明显不同）
            color = (205, 205, 210)
        else:
            color = self.paint_color(r, c)
        draw.rectangle([cx - self.geom.cell_w / 2 + 2, cy - self.geom.cell_h / 2 + 2,
                        cx + self.geom.cell_w / 2 - 2, cy + self.geom.cell_h / 2 - 2], fill=color)


def render_fake_board(grid: Sequence[Sequence[int]], rows_clues, cols_clues,
                      cell: int = 34) -> Tuple[Image.Image, Geometry]:
    """渲染一张「像真实游戏」的合成截图：提示数字右对齐、间距略小于格宽。"""
    n_rows, n_cols = len(grid), len(grid[0])
    max_r = (n_cols + 1) // 2
    max_c = (n_rows + 1) // 2
    clue_pitch = cell * 0.85          # 真实游戏里提示数字间距 ≈ 0.85 格宽
    x1 = max_r * clue_pitch + 48
    y1 = max_c * clue_pitch + 48
    width = int(x1 + n_cols * cell + 40)
    height = int(y1 + n_rows * cell + 40)
    img = Image.new("RGB", (width, height), (26, 26, 38))
    draw = ImageDraw.Draw(img)
    font = _find_test_font(int(cell * 0.55))
    empty = (250, 252, 255)
    line_color = (168, 190, 215)
    digit_color = (72, 78, 120)
    band_a, band_b = (232, 246, 240), (255, 255, 255)

    def cell_box(cx, cy, w, h):
        return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]

    # 提示条带的交替底色（与棋盘行/列对齐）
    for r in range(n_rows):
        yc = y1 + r * cell
        draw.rectangle(cell_box(x1 - max_r * clue_pitch / 2, yc, max_r * clue_pitch, cell),
                       fill=band_a if r % 2 else band_b)
    for c in range(n_cols):
        xc = x1 + c * cell
        draw.rectangle(cell_box(xc, y1 - max_c * clue_pitch / 2, cell, max_c * clue_pitch),
                       fill=band_a if c % 2 else band_b)
    # 棋盘格与网格线
    for r in range(n_rows):
        for c in range(n_cols):
            draw.rectangle(cell_box(x1 + c * cell, y1 + r * cell, cell, cell), fill=empty)
    for r in range(n_rows + 1):
        y = y1 - cell / 2 + r * cell
        draw.line([x1 - cell / 2, y, x1 + (n_cols - 0.5) * cell, y], fill=line_color)
    for c in range(n_cols + 1):
        x = x1 - cell / 2 + c * cell
        draw.line([x, y1 - cell / 2, x, y1 + (n_rows - 0.5) * cell], fill=line_color)

    def put(cx, cy, text):
        box = draw.textbbox((0, 0), text, font=font, stroke_width=1)
        draw.text((cx - (box[2] - box[0]) / 2 - box[0], cy - (box[3] - box[1]) / 2 - box[1]),
                  text, font=font, fill=digit_color, stroke_width=1, stroke_fill=digit_color)

    for r in range(n_rows):
        for i, v in enumerate(reversed(rows_clues[r])):
            put(x1 - cell / 2 - (i + 0.5) * clue_pitch, y1 + r * cell, str(v))
    for c in range(n_cols):
        for i, v in enumerate(reversed(cols_clues[c])):
            put(x1 + c * cell, y1 - cell / 2 - (i + 0.5) * clue_pitch, str(v))
    # 干扰元素（UI 数字），放在右下角，确保识别不会误读
    big = _find_test_font(int(cell * 0.8))
    draw.text((width - 150, height - int(cell * 1.4)), "12:34", font=big, fill=(255, 255, 255))
    draw.rectangle([width - 40, height - int(cell * 1.5), width - 12, height - int(cell * 1.5) + 30],
                   outline=(120, 120, 140))
    geom = Geometry(x1=x1, y1=y1, x2=x1 + (n_cols - 1) * cell, y2=y1 + (n_rows - 1) * cell,
                    cols=n_cols, rows=n_rows)
    return img, geom


def render_fake_special_list(width: int = 1280, height: int = 720,
                             solved: Optional[set] = None,
                             with_frame: bool = True) -> Image.Image:
    """画一张合成的「特别谜题」选关界面（自检/探针用，不依赖游戏截图）。

    solved = 已经解开的格子 (行,列) 集合：这些格子画成画作色，其余画成灰「?」挡板。
    with_frame=False 时不画左边那块信息面板（用来模拟「不是这一屏」）。
    """
    solved = solved or set()
    img = Image.new("RGB", (width, height), (254, 248, 201))
    d = ImageDraw.Draw(img)
    sx, sy = width / 1280.0, height / 720.0
    if with_frame:
        d.rectangle((60 * sx, 230 * sy, 620 * sx, 700 * sy), fill=(38, 60, 120))
        d.rectangle((80 * sx, 245 * sy, 400 * sx, 500 * sy), fill=(255, 251, 222))
    px, py = SPECIAL_GRID_PITCH * sx, SPECIAL_GRID_PITCH * sy
    x0, y0 = SPECIAL_GRID_X * sx, SPECIAL_GRID_Y * sy
    glyph = (230, 231, 231)
    for r in range(SPECIAL_GRID_ROWS):
        for c in range(SPECIAL_GRID_COLS):
            xa, ya = x0 + px * c, y0 + py * r
            xb, yb = xa + px - 1, ya + py - 1
            solved_cell = (r, c) in solved
            d.rectangle((xa, ya, xb, yb), fill=(168, 236, 208) if solved_cell else (187, 187, 187))
            if solved_cell:
                continue
            cx, cy = xa + px * 0.5, ya + py * 0.5
            d.arc((cx - px * 0.16, cy - py * 0.26, cx + px * 0.16, cy + py * 0.04),
                  150, 400, fill=glyph, width=max(2, int(px * 0.08)))
            d.ellipse((cx - px * 0.05, cy + py * 0.12, cx + px * 0.05, cy + py * 0.22),
                      fill=glyph)
    # 内部格线：真机上每个格子（不管灰挡板还是画作）的边界都画着一条分隔线，
    # 识别就是靠这 8 条线认出「这屏是特别谜题方块阵」的（见 _special_boundary_lines）。
    line = (149, 149, 149)
    for k in range(1, SPECIAL_GRID_COLS):
        x = int(round(x0 + px * k))
        d.line([(x, y0), (x, y0 + py * SPECIAL_GRID_ROWS - 1)], fill=line)
    for k in range(1, SPECIAL_GRID_ROWS):
        y = int(round(y0 + py * k))
        d.line([(x0, y), (x0 + px * SPECIAL_GRID_COLS - 1, y)], fill=line)
    return img


def _random_puzzle(n: int, fill_rate: float = 0.55):
    grid = [[1 if random.random() < fill_rate else 0 for _ in range(n)] for _ in range(n)]
    if not any(any(r) for r in grid):
        grid[0][0] = 1
    return _clues_of(grid)


def _picture_puzzle(n: int):
    """生成接近游戏实际出题的图案（随机矩形/圆/斜线），比纯随机噪声好解得多。"""
    grid = [[0] * n for _ in range(n)]
    for _ in range(random.randint(3, 6)):
        kind = random.choice(("rect", "rect", "circle", "line"))
        x0, y0 = random.randint(0, n - 1), random.randint(0, n - 1)
        w, h = random.randint(2, max(2, n // 2)), random.randint(2, max(2, n // 2))
        if kind == "circle":
            cx, cy = random.randint(0, n - 1), random.randint(0, n - 1)
            rr = random.randint(2, max(2, n // 3))
            for r in range(n):
                for c in range(n):
                    if (r - cy) ** 2 + (c - cx) ** 2 <= rr * rr:
                        grid[r][c] = 1
        elif kind == "line":
            length = random.randint(3, n)
            dr, dc = random.choice(((0, 1), (1, 0), (1, 1), (1, -1)))
            r, c = y0, x0
            for _ in range(length):
                if 0 <= r < n and 0 <= c < n:
                    grid[r][c] = 1
                r += dr
                c += dc
        else:
            for r in range(y0, min(n, y0 + h)):
                for c in range(x0, min(n, x0 + w)):
                    grid[r][c] = 1
    if not any(any(r) for r in grid):
        grid[n // 2][n // 2] = 1
    return _clues_of(grid)


def _clues_of(grid):
    n_rows, n_cols = len(grid), len(grid[0])
    rows_clues = [clues_of_line(grid[r]) for r in range(n_rows)]
    cols_clues = [clues_of_line([grid[r][c] for r in range(n_rows)]) for c in range(n_cols)]
    return grid, rows_clues, cols_clues


def _solution_matches(got, grid, rcl, ccl) -> bool:
    """只校验「解满足提示」——随机题可能有多解，此时也算求解器正确。"""
    if got is None:
        return False
    n_rows, n_cols = len(grid), len(grid[0])
    if [clues_of_line(got[r]) for r in range(n_rows)] != [list(c) for c in rcl]:
        return False
    return all(clues_of_line([got[r][c] for r in range(n_rows)]) == list(ccl[c])
               for c in range(n_cols))


def selftest(log=print) -> int:
    random.seed(20260926)
    failures = 0

    log("== 1/4 求解器压测（噪声题 + 图案题） ==")
    cases = []
    for _ in range(10):
        n = random.choice((5, 10, 15))
        cases.append(("噪声", *_random_puzzle(n, random.choice((0.35, 0.5, 0.65)))))
    for _ in range(10):
        n = random.choice((10, 15, 20, 25))
        cases.append(("图案", *_picture_puzzle(n)))
    slowest = 0.0
    for i, (kind, grid, rcl, ccl) in enumerate(cases):
        n = len(grid)
        t0 = time.time()
        got = PuzzleSolver(rcl, ccl).solve(20.0)
        dt = time.time() - t0
        slowest = max(slowest, dt)
        if not _solution_matches(got, grid, rcl, ccl):
            failures += 1
            log(f"  [失败] {kind} {n}×{n} 用例 {i + 1}（{dt:.2f}s）")
        elif dt > 1.0 or n >= 20:
            log(f"  {kind} {n}×{n} 用例 {i + 1} 通过（{dt:.2f}s）")
    log(f"  共 {len(cases)} 例，最慢 {slowest:.2f}s")

    log("== 2/4 棋盘自动定位 / 尺寸推断 ==")
    for n in SQUARE_SIZES:
        grid, rcl, ccl = _random_puzzle(n, 0.5)
        img, geom = render_fake_board(grid, rcl, ccl)
        cands = auto_detect_candidates(img)
        auto = cands[0] if cands else None
        # 自动定位按设计只产出游戏支持的方形尺寸（5/10/15/20/25）：真实游戏里没有
        # 6×6~19×19 这种棋盘，那些尺寸靠「点击推断」认出来。所以这里分两档判定——
        # 标准尺寸必须精确命中且排第一；非标准尺寸只要求候选不外泄非标准尺寸。
        # 两档都要求点击推断给出精确尺寸。
        if n in STANDARD_SIZES:
            auto_ok = (auto is not None and auto.rows == n and auto.cols == n
                       and abs(auto.cell_w - geom.cell_w) < 1.5)
            auto_note = f"首选候选 {auto.rows if auto else '-'}×{auto.cols if auto else '-'}"
        else:
            auto_ok = all(c.rows == c.cols and c.rows in STANDARD_SIZES for c in cands)
            auto_note = f"非游戏尺寸，候选 {len(cands)} 个（只应给标准尺寸）"
        clicks = detect_size_from_clicks(img, geom.x1, geom.x2, "x")
        click_ok = clicks is not None and clicks[0] == n and abs(clicks[1] - geom.cell_w) < 1.0
        if not (auto_ok and click_ok):
            failures += 1
        if clicks:
            log(f"  {n}×{n} -> 自动定位 {'通过' if auto_ok else '失败'}（{auto_note}）"
                f"，点击推断 {'通过' if click_ok else '失败'}"
                f"（{clicks[0]} 格，格宽 {clicks[1]:.1f}px/真值 {geom.cell_w:.1f}px）")
        else:
            log(f"  {n}×{n} -> 自动定位 {'通过' if auto_ok else '失败'}（{auto_note}）"
                f"，点击推断 失败")

    log("== 3/4 关卡列表布局（普通谜题 / 特别谜题） ==")
    # 这一段会消耗随机数，而第 4 段的合成题面是「固定种子 → 固定题」，所以先把随机状态存起来，
    # 跑完再还回去，保证第 4 段的用例跟以前一模一样。
    _rng_state = random.getstate()
    for label, w, h, solved, want_plates in (
            ("1920×1080 全新的一页", 1920, 1080, set(), 25),
            ("1920×1080 解开 5 格", 1920, 1080, {(0, 0), (0, 1), (0, 2), (0, 3), (1, 0)}, 20),
            ("1280×720 只剩 5 格", 1280, 720, {(r, c) for r in range(4) for c in range(5)}, 5)):
        img = render_fake_special_list(w, h, solved)
        set_list_mode(LIST_MODE_AUTO)
        info = detect_special_grid(img)
        kind = _list_kind(img)
        got = int(info["n_plate"]) if info else -1
        ok = kind == LIST_MODE_SPECIAL and got == want_plates
        if not ok:
            failures += 1
        log(f"  {label} -> {kind or '不是列表'}（没解开 {got} 格，应为 {want_plates} 格）"
            f" {'通过' if ok else '失败'}")
    # 一页 25 格全解开（方块阵里一个灰挡板都没有）也得照样认出这一屏——脚本要靠它去翻页。
    # 靠的是「内部 8 条格线明显」这条正面判据，跟解开多少格无关（冷启动一样认得出）。
    for label, w, h in (("1280×720", 1280, 720), ("1920×1080", 1920, 1080)):
        finished = render_fake_special_list(w, h,
                                           {(r, c) for r in range(5) for c in range(5)})
        set_list_mode(LIST_MODE_AUTO)
        kind = _list_kind(finished)
        info = detect_special_grid(finished)
        strong, total = _special_boundary_lines(finished)
        ok = kind == LIST_MODE_SPECIAL and int((info or {"n_plate": 0})["n_plate"]) == 0
        if not ok:
            failures += 1
        log(f"  {label} 一页 25 格全解开 -> {kind or '不是列表'}"
            f"（内部格线明显 {strong}/{total}） {'通过' if ok else '失败'}")
    # 普通列表的判据不许被特别谜题影响：开关 normal 时，特别谜题界面一律当「不是列表」
    set_list_mode(LIST_MODE_NORMAL)
    kind = _list_kind(render_fake_special_list(1280, 720, set()))
    ok = kind == ""
    if not ok:
        failures += 1
    log(f"  开关 normal：特别谜题界面 -> {kind or '不是列表'} {'通过' if ok else '失败'}")
    # 关卡内棋盘不许被当成列表（自动 / 强制特别谜题两种开关都试）
    for n in (5, 10, 15, 20):
        grid, rcl, ccl = _random_puzzle(n, 0.5)
        img, _g = render_fake_board(grid, rcl, ccl)
        set_list_mode(LIST_MODE_AUTO)
        a = _list_kind(img)
        set_list_mode(LIST_MODE_SPECIAL)
        b = _list_kind(img)
        ok = a == "" and b == ""
        if not ok:
            failures += 1
        log(f"  {n}×{n} 棋盘 -> 自动「{a or '不是列表'}」"
            f" / 强制特别谜题「{b or '不是列表'}」 {'通过' if ok else '失败'}")
    # 棋盘点击审计的下标口径：拿格子中心坐标反推，必须还原出原来那一格（0 基）。
    # 2026-10-01 修过一个「审计按 1 基算、计划清单按 0 基存」的 bug——真机跑特别谜题时
    # 58 次计划内的点击被误报成 39 次「点歪了」；探针 debug/probe_click_audit.py 覆盖更多。
    audit_ok = True
    for rows, cols in ((5, 5), (15, 15), (20, 20)):
        gg = Geometry(x1=100.0, y1=50.0, x2=100.0 + (cols - 1) * 37.0,
                      y2=50.0 + (rows - 1) * 37.0, cols=cols, rows=rows)
        for r in range(rows):
            for c in range(cols):
                if _board_indices(gg, *gg.cell_center(r, c)) != (r, c):
                    audit_ok = False
    if not audit_ok:
        failures += 1
    log(f"  点击审计下标口径（坐标反推还原出原来那一格） {'通过' if audit_ok else '失败'}")
    set_list_mode(LIST_MODE_AUTO)
    random.setstate(_rng_state)

    log("== 4/4 合成画面端到端（识别 -> 求解 -> 点击 -> 回读） ==")
    if not init_ocr():
        log("  [跳过] 没有可用的 Tesseract-OCR")
        return failures
    for n in (5, 10, 12, 15, 16, 20):
        grid, rcl, ccl = _picture_puzzle(n) if n >= 15 else _random_puzzle(n, 0.5)
        img, geom = render_fake_board(grid, rcl, ccl)
        fake = FakeScreen(img, geom, (250, 252, 255),
                          lambda r, c: (120 + (r * 7) % 90, 90 + (c * 11) % 110, 200 - (r + c) % 60))
        cfg = Config()
        t0 = time.time()
        try:
            puzzle = read_puzzle(fake, geom, cfg, log)
        except BotAbort as exc:
            failures += 1
            log(f"  [失败] {n}×{n} 识别失败：{exc}")
            continue
        ocr_ok = [puzzle.rows == [list(c) for c in rcl], puzzle.cols == [list(c) for c in ccl]]
        if not all(ocr_ok):
            failures += 1
            log(f"  [失败] {n}×{n} OCR 与真值不一致")
            log(f"    识别行={puzzle.rows}")
            log(f"    真值行={[list(c) for c in rcl]}")
            continue
        sol = PuzzleSolver(puzzle.rows, puzzle.cols).solve(20.0)
        if not _solution_matches(sol, grid, rcl, ccl):
            failures += 1
            log(f"  [失败] {n}×{n} 求解结果不满足提示")
            continue
        if sol != grid:
            log(f"  [提示] {n}×{n} 该随机题存在多解，解与生成图不同（真实游戏题唯一解）")
        cells = fill_cells(sol)
        before = fake.grab_client()
        stop = StopController(cfg)
        execute_fill(fake, geom, cells, cfg, stop, log)
        bad, bad_cells = verify_and_fix(fake, geom, sol, before, cfg, stop, log)
        filled_ok = all(fake.img.getpixel((int(geom.cell_center(r, c)[0]),
                                           int(geom.cell_center(r, c)[1]))) != (58, 58, 82)
                        for r, c in cells)
        if bad or not filled_ok:
            failures += 1
            log(f"  [失败] {n}×{n} 填格校验异常：bad={bad} {bad_cells[:5]}")
        else:
            log(f"  {n}×{n} 全流程通过（{time.time() - t0:.1f}s，填 {len(cells)} 格）")

    log("")
    log("自检结果：" + ("全部通过" if failures == 0 else f"{failures} 项失败"))
    return failures


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Hatsune Miku Logic Paint S 自动闯关脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="紧急停止：F8 / Esc / 鼠标甩到屏幕左上角")
    ap.add_argument("--calibrate", action="store_true", help="重新校准棋盘位置与尺寸")
    ap.add_argument("--manual", action="store_true", help="校准时使用鼠标 F8 手动模式（默认自动定位）")
    ap.add_argument("--loop", nargs="?", type=int, const=0, default=None, metavar="N",
                    help="连续闯关（N 为关数，省略表示一直打）")
    ap.add_argument("--dry-run", action="store_true", help="只识别+求解，不点击")
    ap.add_argument("--probe", action="store_true", help="诊断当前画面并保存标注截图")
    ap.add_argument("--goto", metavar="R,C", default=None,
                    help="把鼠标移到指定格子中心（默认 0,0 即左上角第一格），然后退出")
    ap.add_argument("--selftest", action="store_true", help="不需要游戏的自检")
    ap.add_argument("--confirm", action="store_true", help="每次填格前需要按 Enter 确认")
    ap.add_argument("--mark-empty", dest="mark_empty", action="store_true", default=None,
                    help="填色前用右键把该空着的格子打叉（默认开启）")
    ap.add_argument("--no-mark-empty", dest="mark_empty", action="store_false",
                    help="只用左键填色，不给空格打叉")
    # 「重打未满三星的关卡」已按用户要求删除（和上面 --bg 一个道理：旧面板 host 烙在 exe 里
    # 改不了，界面上那个勾选项还会把这个参数传进来）。留着名字只为兼容旧 exe，传了会被忽略。
    ap.add_argument("--no-star-check", dest="check_stars", action="store_false", default=None,
                    help=argparse.SUPPRESS)
    # 「后台模式」（PrintWindow 抓帧）已删除：本游戏只接受真实鼠标输入，窗口被遮挡时
    # 点了也不会落子；而 PrintWindow 抓 Unity 窗口拿到的还是黑/残帧。留着这个开关只是为了
    # 兼容旧面板（旧 exe 的界面里仍有那个勾选项），传了会被忽略。
    ap.add_argument("--bg", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--hide-console", action="store_true", help="启动后把本控制台窗口最小化")
    ap.add_argument("--list-mode", dest="list_mode", default=None, choices=list(LIST_MODES),
                    help="关卡列表布局：auto=自动识别（默认）/ normal=普通谜题列表 / "
                         "special=特别谜题列表")
    ap.add_argument("--window", default=None, help="自定义窗口标题关键字（可多次用逗号分隔）")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    args = ap.parse_args(argv)

    set_dpi_awareness()
    global CONFIG_PATH
    path = Path(args.config)
    CONFIG_PATH = path
    cfg = Config.load(path)
    if args.window:
        cfg.window_keywords = [k.strip() for k in args.window.split(",") if k.strip()]
    if args.mark_empty is not None:
        cfg.mark_empty = args.mark_empty
    if args.list_mode:
        cfg.list_mode = args.list_mode
    # 关卡列表布局开关（普通谜题 / 特别谜题）：自动识别是默认，可用 --list-mode 或配置项强制
    set_list_mode(getattr(cfg, "list_mode", LIST_MODE_AUTO))

    if args.bg:
        print("[提示] 后台模式已删除（本游戏只接受真实鼠标点击，后台抓帧拿不到有效画面），"
              "本次按普通模式运行。")

    if args.selftest:
        return 1 if selftest() else 0

    if args.hide_console:
        minimize_console()

    hwnd = find_game_window(cfg.window_keywords)
    if hwnd is None:
        print("没有找到游戏窗口。请先启动《Hatsune Miku Logic Paint S》。")
        print(f"（当前搜索关键字：{cfg.window_keywords}，可用 --window 指定）")
        return 2

    if args.calibrate:
        try:
            calibrate(path, cfg.window_keywords, manual=args.manual)
            return 0
        except BotAbort as exc:
            print(f"[中止] {exc}")
            return 1

    if cfg.geom is None:
        print("还没有校准数据，正在自动定位当前棋盘（无需手动校准）…")
        if not init_ocr():
            return 2
        scr = Screen(hwnd)
        g = resolve_geometry(scr, cfg, log=print, attempts=6)
        if g is None:
            print("没能在画面上找到棋盘。请先把游戏停在某一关的棋盘界面，再重新运行。")
            print("（也可以用 --calibrate --manual 走鼠标手动校准）")
            return 2
        cfg.geom = g
        try:
            cfg.save(path)
        except Exception:
            pass
        print(f"已自动定位：{g.rows}×{g.cols}（格宽 {g.cell_w:.1f}px），配置已保存。")

    init_ocr()
    screen = Screen(hwnd)
    stop = StopController(cfg)

    def log(msg=""):
        print(msg, flush=True)

    log("=" * 58)
    log("Hatsune Miku Logic Paint S 自动闯关")
    log(f"窗口：{_window_title(hwnd)}")
    log(f"棋盘：{cfg.geom.rows}×{cfg.geom.cols}    格宽 {cfg.geom.cell_w:.1f}px")
    _list_mode_txt = {LIST_MODE_AUTO: "自动识别（普通谜题黄卡片 / 特别谜题灰方块阵）",
                      LIST_MODE_NORMAL: "只认普通谜题列表",
                      LIST_MODE_SPECIAL: "只认特别谜题列表"}
    log(f"关卡列表布局：{_list_mode_txt.get(get_list_mode(), get_list_mode())}")
    log("紧急停止：F8 / Esc / 鼠标甩到屏幕左上角")
    log("=" * 58)
    try:
        if args.goto is not None:
            # 重新识别当前棋盘，确保坐标跟画面一致
            g = resolve_geometry(screen, cfg, log=log, attempts=4)
            if g is not None:
                cfg.geom = g
                try:
                    cfg.save(path)
                except Exception:
                    pass
            spec = (args.goto or "0,0").strip()
            parts = [p for p in re.split(r"[,\s]+", spec) if p != ""]
            try:
                r = int(parts[0]) if len(parts) >= 1 else 0
                c = int(parts[1]) if len(parts) >= 2 else 0
            except ValueError:
                log(f"[错误] --goto 的格子坐标无法解析：{spec!r}（应为 '行,列'，例如 '0,0'）")
                return 2
            r = max(0, min(r, cfg.geom.rows - 1))
            c = max(0, min(c, cfg.geom.cols - 1))
            cx, cy = screen.move_to_cell(cfg.geom, r, c)
            log(f"鼠标已移到第 {r} 行第 {c} 列格子中心（客户区 {cx:.1f}, {cy:.1f}）")
        elif args.probe:
            probe(screen, cfg.geom, cfg, log)
        elif args.dry_run:
            play_one(screen, cfg.geom, cfg, stop, confirm=False, dry_run=True, log=log)
        elif args.loop is not None:
            run_loop(screen, cfg.geom, cfg, stop, args.loop,
                     confirm=args.confirm or not cfg.auto_confirm, log=log)
        else:
            play_one(screen, cfg.geom, cfg, stop,
                     confirm=args.confirm or not cfg.auto_confirm, dry_run=False, log=log)
            log("本关完成。连续闯关请加 --loop")
        return 0
    except BotAbort as exc:
        log(f"[中止] {exc}")
        return 1
    except KeyboardInterrupt:
        log("[中止] 用户中断（Ctrl+C）")
        return 1


if __name__ == "__main__":
    sys.exit(main())