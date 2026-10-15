# Reasonix 状态栏 mp3 播放器

给 [Reasonix](https://reasonix.io) 桌面版加一个贴在**底部状态栏**的 mp3 播放器：

```
🎵  |  🔀 / 🔁  |  ＋
```

> **本仓库不包含任何 Reasonix 源码。** 其中"前端微调"部分是可复现的**脚本**——它在你自己机器上，对你本机安装的 Reasonix 前端产物做一次精确字符串替换，仓库里不携带任何第三方代码。

## 效果

| 位置 | 交互 |
| --- | --- |
| 低栏 `🎵` | 点击 = 播放 / 停止（随机模式下开始播放会随机选一首） |
| 低栏 `🔀 / 🔁` | 点击 = 切换「随机循环 / 顺序循环」并立即随机换一首；**悬停显示当前曲目** |
| 低栏 `＋` | 弹出 macOS 原生文件选择器（只筛 `.mp3`、可多选、默认定位当前曲目所在文件夹） |
| 对话框片 | `▶播放/停止`、`🔀/🔁`、`＋添加 mp3` |

- 手动添加的曲目持久保存在 `~/.reasonix/mp3-player/playlist.json`，与当前工作区的 `*.mp3` 扫描结果合并去重
- 播放用 macOS 自带 `afplay`；一首放完自动续播（随机循环 / 顺序循环）

## 目录结构

```
reasonix_mp3_plugin/
├── plugin/                      # Reasonix 扩展（sidecar）
│   ├── reasonix-plugin.json     # Manifest v2
│   ├── sidecar.py               # 播放器实现（纯 Python 标准库）
│   └── selftest_host.py         # 自测（模拟 host 走完整协议流程）
├── frontend/                    # 前端微调（可选，让低栏图标可点击）
│   ├── make-frontend-patch.py   # 从你本机的 Reasonix 生成补丁
│   └── patch-reasonix-player.sh # 应用补丁 + 重签名
└── docs/
    └── LESSONS.md               # 实战经验与排错笔记
```

## 安装

### 1) 安装插件（sidecar）

```bash
# 用官方 CLI（若你的环境能正常写 ~/.reasonix）
reasonix plugin install "$(pwd)/plugin" --link --replace --yes

# 或在 Reasonix 桌面里用官方安装工具 install_source（kind=plugin, source=<本仓库 plugin 目录>）
reasonix plugin doctor mp3-player     # 验证
```

### 2) 应用前端补丁（可选，让低栏图标变成可点击）

> 不做这一步，低栏图标只是**只读显示**；播放控制改在对话里的卡片按钮上。

```bash
# 先完全退出 Reasonix（⌘Q）
bash frontend/patch-reasonix-player.sh
# 首次会弹「终端 想要修改其他 App」→ 允许；随后重开 Reasonix
```

还原：脚本末尾会打印还原命令（`cp <target>.orig <target> && codesign ...`）。

### 3) 重启 Reasonix

插件改动需要**完全重启 app**（⌘Q → 重开）才生效。注意：**别用 `/reload`**——会话进行中 reload 会被回滚，还会清空扩展 UI。

## 使用

- 把要听的 mp3 放进当前会话的工作区目录，或点 `＋` 用系统选择器添加
- 低栏 `🎵` 播放/停止；`🔀/🔁` 切换模式（切到 `🔀` 会立刻随机换一首）
- 悬停 `🔀/🔁` 看当前放的是哪首

## 工作原理

```
Reasonix 桌面 (host)
   └── Extension Protocol v2 sidecar（plugin/sidecar.py, 纯 stdlib）
         ├── status 表面  → 底部状态栏的 🎵 chip（只读）
         ├── card 表面    → 对话里的按钮卡片
         └── 本机调 afplay / osascript 完成播放与选文件
```

- **不依赖任何第三方库**：sidecar 手写 JSON-RPC over NDJSON，走 Reasonix 的 `reasonix.extension.v2` 协议
- **前端补丁**只做一件事：给状态栏的扩展 chip 挂上 `onClick → InvokeExtensionAction(...)`

细节与踩坑记录见 [`docs/LESSONS.md`](docs/LESSONS.md)。

## 免责声明

- 本项目按"现状"提供，仅供学习与自用；使用风险自负。
- 修改 `/Applications/Reasonix.app` 的打包产物会**破坏其代码签名**（脚本会用 ad-hoc 签名修复），且 **Reasonix 升级后补丁会被覆盖**。
- 本仓库不含 Reasonix 源码；请遵守 Reasonix 自身的许可条款。

## License

MIT
