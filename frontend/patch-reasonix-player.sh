#!/bin/bash
# 让 Reasonix 底部状态栏的扩展图标变成可点击控件：
#   点击 🎵 = 播放/停止   |   点击 🔀/🔁 = 切换随机/顺序（并随机换一首）   |   点击 ＋ = 添加 mp3
#
# 用法：先【完全退出】Reasonix（⌘Q），再在「终端」里执行：
#     bash patch-reasonix-player.sh
#
# 首次运行 macOS 会弹「终端 想要修改其他 App」，点【允许】。
# 还原：脚本末尾会打印还原命令。
set -e

APP="${REASONIX_APP:-/Applications/Reasonix.app}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

TARGET="$(ls "$APP"/Contents/Resources/app/assets/AppRuntimeView-*.js 2>/dev/null | head -1 || true)"
[ -n "$TARGET" ] || { echo "✗ 找不到 AppRuntimeView-*.js，请确认 Reasonix 装在：$APP"; exit 1; }

echo "[1/4] 生成补丁（读你本机的 Reasonix，不联网）"
python3 "$HERE/make-frontend-patch.py" "$TMP/patched.js" "$APP"

if [ -f "$TARGET.orig" ]; then
  echo "[2/4] 原版备份已存在，跳过：$TARGET.orig"
else
  echo "[2/4] 备份原文件 → $TARGET.orig"
  cp "$TARGET" "$TARGET.orig"
fi

echo "[3/4] 写入补丁"
cp "$TMP/patched.js" "$TARGET"

echo "[4/4] 重新签名（ad-hoc）"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 && echo "    签名完成" \
  || echo "    （签名命令有提示，一般可忽略）"

echo
echo "✅ 完成，请重新打开 Reasonix。"
echo
echo "还原命令："
echo "  cp \"$TARGET.orig\" \"$TARGET\" && codesign --force --deep --sign - \"$APP\""
