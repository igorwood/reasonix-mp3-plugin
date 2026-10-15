#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Reasonix 低栏补丁。

作用：把 Reasonix 底部状态栏里的「扩展 status chip」改成可点击的三段式控件
      （播放/停止 | 模式切换 | 添加文件），并让悬停显示详情。

说明：本脚本**不包含任何 Reasonix 源码**。它只在你自己的电脑上，
      对**你本机安装的** Reasonix 前端文件做一次精确的字符串替换。

用法：
    python3 make-frontend-patch.py <输出文件> [Reasonix.app 路径]
"""

import glob
import os
import sys

DEFAULT_APP = "/Applications/Reasonix.app"

# Reasonix 原版中「扩展 status chip」的渲染片段（用于精确匹配）
OLD = (
    '(0,sn.jsxs)("span",{className:`stat statusbar__extension '
    'statusbar__extension--${n}`,children:[(0,sn.jsx)(is,{size:12,"aria-hidden":"true"}),'
    '(0,sn.jsx)("span",{className:"statusbar__extension-label",children:e.label}),'
    'void 0!==s?(0,sn.jsxs)("b",{children:[s,"%"]}):null]})'
)

# 替换后的版本：三个可点区域，分别触发 /<plugin>:toggle|mode|add
NEW = (
    '(0,sn.jsxs)("span",{className:`stat statusbar__extension '
    'statusbar__extension--${n}`,children:['
    '(0,sn.jsx)("span",{role:"button",title:"播放 / 停止",style:{cursor:"pointer",'
    'display:"inline-flex",alignItems:"center"},onClick:t=>{t.stopPropagation();'
    'h.InvokeExtensionAction("","/"+e.pluginId+":toggle",{}).catch(()=>{})},children:['
    '(0,sn.jsx)(is,{size:12,"aria-hidden":"true"}),'
    '(0,sn.jsx)("span",{className:"statusbar__extension-label",children:e.label})]}),'
    '(0,sn.jsx)("span",{role:"button",title:e.detail||"",style:{cursor:"pointer",'
    'marginLeft:6,opacity:.85},onClick:t=>{t.stopPropagation();'
    'h.InvokeExtensionAction("","/"+e.pluginId+":mode",{}).catch(()=>{})},'
    'children:-1!==(e.detail||"").indexOf("随机")?"🔀":"🔁"}),'
    'void 0!==s?(0,sn.jsxs)("b",{children:[s,"%"]}):null,'
    '(0,sn.jsx)("span",{role:"button",title:"添加 mp3",style:{cursor:"pointer",'
    'marginLeft:6},onClick:t=>{t.stopPropagation();'
    'h.InvokeExtensionAction("","/"+e.pluginId+":add",{}).catch(()=>{})},children:"＋"})]})'
)

# 已打过补丁的标志串
MARK = '"/"+e.pluginId+":mode"'


def find_target(app):
    pat = os.path.join(app, "Contents", "Resources", "app", "assets", "AppRuntimeView-*.js")
    hits = sorted(glob.glob(pat))
    if not hits:
        sys.exit("[错误] 找不到 %s（Reasonix 未安装或目录不同）" % pat)
    return hits[0]


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "patched-AppRuntimeView.js"
    app = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_APP
    target = find_target(app)
    src = open(target, encoding="utf-8").read()
    if MARK in src:
        sys.exit("[跳过] 目标文件似乎已打过补丁。")
    n = src.count(OLD)
    if n != 1:
        sys.exit("[错误] 特征片段匹配 %d 次（期望 1）—— Reasonix 版本可能不同，需重新适配。" % n)
    open(out, "w", encoding="utf-8").write(src.replace(OLD, NEW))
    print("已生成补丁：%s" % out)
    print("（目标文件参考：%s）" % target)


if __name__ == "__main__":
    main()
