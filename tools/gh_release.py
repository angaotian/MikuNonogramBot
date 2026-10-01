#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用本机已保存的 GitHub 凭据（Git Credential Manager）建 Release 并上传产物。

用法：
  python tools/gh_release.py --tag v1.0.0 --notes dist\\<包>\\docs\\发布说明.md \\
      dist\\MikuNonogramBot-1.0.0-win64.zip dist\\MikuNonogramBot-1.0.0-src.zip
  python tools/gh_release.py --tag v1.0.0 --dry-run <同上的 zip...>

凭据：`git credential fill`（就是 push 时让你登录的那份），**脚本只把它放进内存里当
Authorization 头，绝不打印、绝不落盘**；仓库地址默认取最近 push 上去的那个 origin。
Release 已存在时不会报错，而是复用它、只补缺的附件（可反复跑）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"


def get_token() -> str:
    """从 git 的凭据管理器里取 github.com 的 token（不打印）。"""
    out = subprocess.run(["git", "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, timeout=120)
    for line in out.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("拿不到 GitHub 凭据：先在某个仓库里 git push 一次，让 GCM 记住登录。")


def repo_from_origin(src_dir: Path) -> str:
    out = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(src_dir),
                         capture_output=True, text=True, timeout=60)
    url = out.stdout.strip()
    if not url:
        raise SystemExit("这个目录没有 origin：%s" % src_dir)
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):                      # git@github.com:owner/name
        url = url.split(":", 1)[1]
    elif "github.com/" in url:                      # https://github.com/owner/name
        url = url.split("github.com/", 1)[1]
    return url.split("/", 2)[0] + "/" + url.split("/", 2)[1]


def _multipart(field: str, filename: str, data: bytes,
               ctype: str = "application/octet-stream"):
    """拼一个 multipart/form-data 体（GitHub 的附件上传接口要这个格式）。

    ⚠ 直接把文件字节当 body 发（Content-Type: application/zip）会被回
    `HTTP 400 Multipart form data required`——实测就是这么翻车的。
    """
    boundary = "----HermesReleaseBoundary7d1f3a"
    head = ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
            "Content-Type: %s\r\n\r\n" % (boundary, field, filename, ctype)).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    return head + data + tail, "multipart/form-data; boundary=%s" % boundary


def upload_asset(rid: int, path: Path, token: str, repo: str):
    """上传一个附件：两种形式都试（GitHub 这两种在不同版本上表现不一样）。

      ① 原始字节 + `Content-Type: application/octet-stream`（官方 curl 示例的写法）
      ② multipart/form-data（字段名 file）

    ⚠ 两个坑都真踩过：
      · **上传地址必须是 `uploads.github.com/repos/<owner>/<repo>/releases/<id>/assets`** ——
        漏掉 `/repos/<owner>/<repo>` 那一段时，GitHub 会回一堆莫名其妙的
        `400 Multipart form data required` / `422 Bad Size`，看着像格式问题，其实是路径错了。
      · `?name=` 要用**文件名**（含扩展名），和 multipart 里的 filename 一致。

    返回 (ok, 说明)。
    """
    name = path.name
    data = path.read_bytes()
    path_tpl = "/repos/%s/releases/%d/assets?name=%s" % (repo, rid, name)
    tries = [
        ("原始字节(octet-stream)", data, "application/octet-stream"),
        ("multipart", *_multipart("file", name, data)),
    ]
    last = ""
    for label, body, ctype in tries:
        st, res = api("POST", path_tpl, token, raw=(body, ctype), base=UPLOADS)
        if st in (200, 201):
            return True, "%s：%s（%.1f MB，state=%s）" % (
                label, res.get("name"), res.get("size", 0) / 1048576, res.get("state"))
        errs = res.get("errors")
        last += "%s%s → HTTP %s %s%s" % ("；" if last else "", label, st,
                                         res.get("message"),
                                         (" " + json.dumps(errs, ensure_ascii=False)) if errs else "")
    return False, last


def api(method: str, path: str, token: str, data=None, ctype="application/json",
        base: str = API, raw=None):
    """raw=(bytes, content_type) 时直接用它当 body（multipart 上传附件用）。"""
    body = None
    headers = {"Authorization": "Bearer %s" % token,
               "Accept": "application/vnd.github+json",
               "User-Agent": "make-release-script"}
    if raw is not None:
        body, ctype_raw = raw
        headers["Content-Type"] = ctype_raw
    elif data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            body = data
            headers["Content-Type"] = ctype
    req = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        try:
            return exc.code, json.loads(raw or "{}")
        except Exception:
            return exc.code, {"message": raw[:400]}


def main() -> int:
    ap = argparse.ArgumentParser(description="建 GitHub Release 并上传附件（用 GCM 里的凭据）")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--notes", default="", help="Release 正文文件（markdown）")
    ap.add_argument("--src-dir", default="", help="含 origin 的源码树目录（默认找 dist/*-src）")
    ap.add_argument("--repo", default="", help="owner/repo；不给就从 --src-dir 的 origin 猜")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-only", action="store_true",
                    help="只核对远端：Release 正文/附件 + 每个附件真去 HEAD 一次下载链接")
    ap.add_argument("--replace", action="store_true",
                    help="同名附件已存在时先删掉再传（默认是跳过）")
    ap.add_argument("--delete", action="store_true",
                    help="删掉这个 tag 的 Release（连附件一起；仓库里的 git tag 不动）")
    ap.add_argument("assets", nargs="*", help="要上传的文件")
    args = ap.parse_args()

    src = Path(args.src_dir) if args.src_dir else next(iter(sorted((ROOT / "dist").glob("*-src"))), None)
    if args.repo:
        repo = args.repo
    else:
        if not src:
            raise SystemExit("找不到 dist/*-src 目录（用 --src-dir 指定，或直接给 --repo owner/name）")
        repo = repo_from_origin(src)
    title = args.title or args.tag
    body = Path(args.notes).read_text(encoding="utf-8") if args.notes else ""
    assets = [Path(a) for a in args.assets]

    print("仓库：%s" % repo)
    print("Tag ：%s（%s）" % (args.tag, title))
    print("正文：%s（%d 字）" % (args.notes or "(空)", len(body)))
    for a in assets:
        print("附件：%s（%.1f MB）%s" % (a, a.stat().st_size / 1048576,
                                        "" if a.exists() else "  ← 文件不存在！"))
    if args.dry_run:
        print("\n--dry-run：什么都没提交。")
        return 0

    token = get_token()

    if args.delete:
        st, rel = api("GET", "/repos/%s/releases/tags/%s" % (repo, args.tag), token)
        if st != 200:
            print("没找到 %s 的 Release（HTTP %s），不用删。" % (args.tag, st))
            return 0
        st2, res = api("DELETE", "/repos/%s/releases/%d" % (repo, rel["id"]), token)
        ok = st2 in (200, 204)
        print("删除 Release %s：HTTP %s %s"
              % (args.tag, st2, (res.get("message", "") if isinstance(res, dict) else "")))
        if ok:
            print("（仓库里的 git tag %s 没动；要一起删就 git push --delete origin %s）"
                  % (args.tag, args.tag))
        return 0 if ok else 2

    if args.verify_only:
        st, rel = api("GET", "/repos/%s/releases/tags/%s" % (repo, args.tag), token)
        if st != 200:
            print("核对失败：HTTP %s %s" % (st, rel.get("message")))
            return 2
        print("Release: %s" % rel["html_url"])
        print("  tag      : %s" % rel["tag_name"])
        print("  标题     : %s" % rel["name"])
        print("  正文     : %d 字%s" % (len(rel.get("body") or ""),
                                        "" if rel.get("body") else "  ← 空的！"))
        for a in rel.get("assets", []):
            url = a["browser_download_url"]
            code, size = 0, ""
            try:
                req = urllib.request.Request(url, method="HEAD",
                                             headers={"User-Agent": "make-release-script"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    code = resp.status
                    size = resp.headers.get("Content-Length", "?")
            except Exception as exc:
                size = "HEAD 失败：%s" % exc
            print("  附件     : %-46s %.1f MB  state=%s" % (a["name"], a["size"] / 1048576, a["state"]))
            print("             下载 HEAD → %s，Content-Length=%s" % (code or "-", size))
            print("             %s" % url)
        return 0

    status, rel = api("POST", "/repos/%s/releases" % repo, token,
                      {"tag_name": args.tag, "name": title, "body": body,
                       "draft": False, "prerelease": False})
    if status == 422:                                # 已经建过 → 复用（正文/标题有变就更新）
        status, rel = api("GET", "/repos/%s/releases/tags/%s" % (repo, args.tag), token)
        if status == 200 and body and rel.get("body") != body:
            rid0 = rel["id"]
            st, rel2 = api("PATCH", "/repos/%s/releases/%d" % (repo, rid0), token,
                           {"body": body, "name": title})
            if st == 200:
                rel = rel2
                print("已更新 Release 正文（%d 字）" % len(body))
    if status not in (200, 201):
        print("建 Release 失败：HTTP %s %s" % (status, rel.get("message")))
        return 2
    rid = rel["id"]
    print("\nRelease: %s" % rel.get("html_url"))
    have = {a["name"]: a for a in rel.get("assets", [])}
    for a in assets:
        if a.name in have:
            if not args.replace:
                print("附件已存在，跳过：%s（要覆盖加 --replace）" % a.name)
                continue
            st, _ = api("DELETE", "/repos/%s/releases/assets/%d" % (repo, have[a.name]["id"]), token)
            print("已删除旧附件：%s（HTTP %s）" % (a.name, st))
        ok, detail = upload_asset(rid, a, token, repo)
        if ok:
            print("上传成功：%s" % detail)
        else:
            print("上传失败：%s\n  %s" % (a.name, detail))
            return 3

    status, rel = api("GET", "/repos/%s/releases/tags/%s" % (repo, args.tag), token)
    if status == 200:
        print("\n复核（重新查一遍远端）：")
        print("  tag    : %s" % rel["tag_name"])
        print("  标题   : %s" % rel["name"])
        print("  页面   : %s" % rel["html_url"])
        for a in rel.get("assets", []):
            print("  附件   : %-46s %.1f MB  %s"
                  % (a["name"], a["size"] / 1048576, a["browser_download_url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
