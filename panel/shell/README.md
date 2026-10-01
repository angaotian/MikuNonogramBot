# 面板外壳（MikuPanel.exe 里除了那三件内容物之外的部分）

**这一层不要改，也改不了。** 它是二进制黑盒，源码不在工程里——PyInstaller 只保留了
字节码，所以这里放的是从 exe 里反汇编出来的行为记录（`app.dis.txt`、`web_panel.dis.txt`），
用来"查它到底怎么跑我们的代码"，而不是用来编辑。

## 它做了什么（从字节码还原）

入口脚本 `app.py`：

| 函数 | 行为 |
|---|---|
| `base_dir()` | 非打包时 = 脚本所在目录；打包后 = **exe 所在目录**，但会先看 `exe目录` 和它的**上一级**里有没有 `miku_logic_paint_bot.py`，有就用那个目录 |
| `resource_dir()` | `sys._MEIPASS`（exe 内部解包目录），拿不到才退回 `base_dir()` |
| `find_bot_source()` | 先 `base_dir()/miku_logic_paint_bot.py`，再 `_MEIPASS/miku_logic_paint_bot.py` |
| `run_bot(rest)` | 把找到的脚本 `compile(..., "exec")` 后在**本进程**里执行，`sys.argv = [真实py路径] + rest`；`SystemExit` 的 code 当返回值 |
| `main()` | `argv[1] == "--_runbot"` → 走 `run_bot`；否则起界面 |

主流程：`base_dir` → 找主脚本（找不到弹 `Miku 控制台` 错误框并退出）→
给 `web_panel` 模块注入 `ROOT / RESOURCE_DIR / BOT_SCRIPT / CONFIG_PATH` 四个全局量 →
`CONFIG_PATH = base_dir/miku_bot_config.json`，**若不存在就从 `_MEIPASS` 里拷一份出来** →
`web_panel` 起本地 HTTP 服务（端口从 **8770** 起试 20 个，都被占用就让系统分配）→
`webview.create_window("Miku 数织自动闯关 · 控制面板")` 开原生窗口。

## 由此推出的三条硬约束（改东西时必须守）

1. **真源位置**：`miku_logic_paint_bot.py` 必须待在 `exe 同级目录`（或它的上一级）。
   面板**优先加载外部 .py**，exe 里那份只是兜底——所以改主脚本 → 重打包 exe 是"为了兜底一致"，
   实际运行时用的就是工作区这份。
2. **配置路径固定**：面板用的永远是 `exe同级/miku_bot_config.json`。
   exe 里那份只在"同级目录还没有配置文件"时被拷出来当地基。
3. **界面只能打进 exe**：`web\index.html` 从 `_MEIPASS`（exe 内部）取，
   改界面**必须**用 `tools/panel_build.py` 重新写进 exe，光改 `panel/index.html` 没用。

## 内部参数

```
MikuPanel.exe --_runbot [主脚本的参数...]      # 面板自己在"跑一关"时用的通道
```

## 界面调用的接口（由 `web_panel` 模块实现）

`/api/start`（POST，带主脚本参数）、`/api/stop`（POST）、`/api/state`（GET）、
`/api/logs?since=N`（GET，增量日志）、`/api/clear`（POST，清日志）。
