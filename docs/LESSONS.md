# 实战经验与排错笔记

给 Reasonix 写"带 UI 的扩展"、以及微调它的前端，一路踩了不少坑。这里按主题记录，便于复用与排错。

## 1. Reasonix 扩展模型（Extension Protocol v2）

- 扩展能提供的能力族：`interceptors` / `strategies` / `providers` / `ui`，以及**代码型 runtime**（sidecar 进程）。
- UI 只有四种表面：`status`（状态栏 chip）、`card`（对话里的卡片）、`form`、`notification`。
- sidecar 由 host 通过 stdio 以 **JSON-RPC 2.0 over NDJSON** 通信，帧上限 8 MiB。

### Manifest（`reasonix-plugin.json`）

最小可用形态：

```json
{
  "apiVersion": "reasonix.io/plugin/v2",
  "name": "mp3-player",
  "version": "0.4.0",
  "description": "...",
  "provides": [
    { "namespace": "plugin/mp3-player", "kind": "interceptors", "id": "default", "version": "0.4.0" }
  ],
  "runtime": {
    "command": "${REASONIX_PLUGIN_ROOT}/sidecar.py",
    "args": [],
    "intercepts": ["session.start", "session.load", "session.rotate"],
    "capabilities": ["interceptors", "ui"]
  }
}
```

坑点：

- **`${REASONIX_PLUGIN_ROOT}` 只在 `command` 里展开，在 `args` 里不展开**。写在 args 会被当相对路径拼到 host 的 cwd，报 `can't open file '.../${REASONIX_PLUGIN_ROOT}/sidecar.py'`。→ 路径放 `command`（脚本要可执行 + shebang），`args` 留空。
- **`provides` 的 `kind` 只接受 `interceptors`**。加 `kind: "uiaction"`、或在顶层写 `uiActions`，都会报 `no installable manifest (detail omitted; N bytes)`（N 正好是该字段的字节数，极难定位）。
- **UI actions 不需要写进 manifest**，`capabilities: ["ui"]` 即可。
- 订阅的事件**必须**在 manifest 的 `intercepts` 里声明，否则握手被拒：`subscribed to "session.start" which its manifest does not intercept`。

## 2. sidecar 的读写时序

- **card 必须在 runtime 构建后「延迟 + 重复」发布**。早期（`extension/initialized` 立刻）发布的 card 会被前端丢弃（transcript 未就绪）；status 因为持续更新反而能存活。做法：initialized 后 `sleep(3)`，再每 2 秒重发 status+card 约 15 次。
- **先回 RPC response，再执行副作用**。处理 `extension/ui/action` 时若先做了会触发 publish 的事，publish 帧会先于 response 写出，让按 id 等响应的调用方错位。
- **`host/ui/publish` 有响应**（成功回 `{"accepted": true}`）。把每次 publish 登记进 pending 表并记日志（`publish ok: card:<id>` / `PUBLISH REJECTED`），就能自检 host 是否接受该表面。

### 多线程注意（sidecar 里常有多个线程）

- `_next_id` **必须加锁**（publish 与 host 请求共用 id 空间，冲突会导致响应错配）。
- `_on_response` 与等待响应的 `_call_host` 对 pending 表的 pop 语义要一致（一方 pop 了另一方就不能再 pop，否则请求结果丢失、超时）。

## 3. host 只认 `uiActions` 声明过的 action

sidecar 除了在 **card.actions** 里列出按钮，**还必须在 `InitializeResult.uiActions` 里声明**每个 actionId。

否则：从低栏/卡片调 `/<plugin>:<action>` 时，host 会**静默丢弃**——日志里 `toggle`/`add` 有到达记录、新增的 `mode` 一条都没有，就是典型症状。

## 4. 前端微调（让只读 chip 可点击）

- Reasonix 桌面的前端是**未混淆、可读的打包 JS**：`/Applications/Reasonix.app/Contents/Resources/app/assets/*.js`
  - 状态栏与扩展卡片渲染在 `AppRuntimeView-*.js`（`ExtensionStatusBarChips` / `ExtensionCard`）
- 卡片按钮的调用链：`h.InvokeExtensionAction(tabId ?? "", "/" + pluginId + ":" + actionId, {})`
  - `h` 是该 chunk 的模块级 import（`import{…t as h…}from"./bridge-*.js"`），同 chunk 任何函数都可用
- 状态栏的扩展 chip 原本**没有 onClick**（只有固定小图标 + 文本 + tooltip）。给它注入几个可点 `span`，分别调 `InvokeExtensionAction("", "/<plugin>:toggle|mode|add", {})` 即可
- 悬停信息用标准的 HTML `title` 属性（`title={e.detail}`）最省事，能显示曲名/序号/模式

### 限制

- **host→前端**的扩展状态里，插件能放的只有 label/detail/severity/progress；卡片里只有 actions 可点。
- macOS 的 **App Management 保护**会阻止普通进程写 `/Applications` 下的其他 App（报 `Operation not permitted`）。所以补丁要**由用户在终端里执行**（首次弹授权），再 `codesign --force --deep --sign -` 重签。
- **升级即失效**：Reasonix 更新会覆盖前端产物，需重跑补丁。

## 5. 系统弹窗：用 osascript 而不是 GUI 库

后台 sidecar 想弹**原生文件选择器**，最省事的是 `osascript`（无需主线程/Tk，任何进程都能调）：

```applescript
set fs to choose file with prompt "选择要添加的 mp3 文件" \
  default location (POSIX file "/path/to/dir") \
  of type {"mp3"} with multiple selections allowed
```

返回的别名用 `POSIX path of` 转成 POSIX 路径。

## 6. 排错入口

| 现象 | 去哪看 |
| --- | --- |
| 插件 UI 完全不出现 | 桌面日志 `~/.reasonix/desktop-shell/logs/service.log`，搜 `extension runtime` 看握手失败原因 |
| 不知道 host 收没收到 publish / action | sidecar 自写日志（本项目写到 `~/.reasonix/mp3-player/sidecar.log`） |
| 改了插件不生效 | 必须**完全重启 app**（⌘Q → 重开）；会话进行中 `/reload` 会回滚并清空扩展 UI |
| 改了 manifest | 需**重新安装**（`install_source` apply=true 或 CLI），link 模式也一样 |

## 7. 自测方法

不依赖真实 host，直接 spawn sidecar、喂 NDJSON 帧（`initialize → initialized → intercept/event → ui/action → shutdown`）逐帧断言即可。见 `plugin/selftest_host.py`。

要点：
- 模拟 host 时，对 sidecar 发来的 `host/ui/publish` 请求要回一个 `{"accepted": true}`
- 按 id 读 response 的循环要能跳过中间穿插的 publish 帧
- 需要用文件选择器之类的外部交互时，用环境变量注入（本项目用 `MP3PLAYER_FAKE_PICK` 指向一个文件，内容即"选中的路径"）
