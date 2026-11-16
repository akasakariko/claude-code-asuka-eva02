# EVA-02 明日香 · Claude Code 主题 + 日语界面

把 Claude Code 的终端界面改成 **EVA-02 弐号機 / 惣流・アスカ・ラングレー** 风格：

- 完整的 EVA-02 配色主题（`/theme` 里显示为 **EVA-02 Asuka**）
- 每个会话随机一条 NERV 战术 HUD 横幅
- 日语 EVA 风格的 spinner 动词（`EVA-02 弐号機・発進`、`あんたバカぁ？！`……）
- EVA-02 同步率状态栏（SYNC 进度条 / A10 / TPS）
- **界面日语化**：启动提示、粘贴提示、快捷键提示、footer（`オート起動` / `shift+tab→切替`）、
  工具摘要（`思考` / `走査` / `実行`）、账户行（`NERV 従量課金`）、斜杠命令说明等（176 条）

> 说明：Claude Code 的“自定义主题”只支持颜色（`name` / `base` / `overrides`），
> 不支持修改任何界面文字；`/config` 里的 Language 只影响 Claude 的回复语言。
> 因此文字部分是通过 `localize/asuka_patch.py` 对原生二进制的字符串表打补丁实现的。

## 已验证环境

- macOS arm64（Apple Silicon）
- Claude Code **2.1.325**（native 单文件构建，Bun bytecode）
- Linux 上同样结构，但 ad-hoc 重签名只在 macOS 执行，未实测

> 只支持 **native 安装**（`~/.local/share/claude/versions/`）。
> 用 npm 安装的 `claude` 是 JS 脚本，不支持。

## 安装

```bash
git clone https://github.com/akasakariko/claude-code-asuka-eva02.git
cd claude-code-asuka-eva02
bash install.sh
```

> 仓库当前是 **private**。在另一台电脑上先 `gh auth login`（或把仓库改成 public）再 clone。

重启 Claude Code 后生效。主题也可以在 `/theme` 里确认选择 **EVA-02 Asuka**。

只想要主题、不想动二进制：

```bash
bash install.sh --no-patch
```

## 卸载日语化 / 还原

```bash
bash install.sh --unpatch
# 或直接：
python3 ~/.claude/asuka/asuka_patch.py --restore
```

备份位于：`<claude 可执行文件>.asuka-orig`。

## 原理（给好奇的人）

Claude Code 的 native 构建里，Bun 把 JS 编译成 bytecode，界面字面量存放在一张共享字符串表：

```
[u32 count][u32 offset[count]]
record = [u32 length | is8Bit<<31][u32 hash][chars][pad-to-4]
```

`asuka_patch.py` 做的是：

1. 找到字符串表里对应的 Latin-1 记录（校验长度 + WebKit RapidHash，避免误伤）；
2. 原地改写成 **UTF-16LE** 日文：清掉 `is8Bit` 位，重算 hash；
3. 新记录严格不超过原记录的 4 字节对齐尺寸 —— 偏移数组和所有 bytecode 引用都保持不变；
4. 在 macOS 上用 `codesign --force --sign -` 重新 ad-hoc 签名；
5. 用 `os.replace` 原子替换，正在运行的进程不受影响。

所以：**记录能装的日文字数约为英文长度的一半**。翻译表里每条的 `ja` 都必须放得下，
`--check` 会逐条报告。

### 自动重打补丁

`install.sh` 会往 `~/.claude/settings.json` 的 `SessionStart` 里加一条 hook：

```
python3 ~/.claude/asuka/asuka_patch.py --auto
```

- 已打过补丁 → 读标记文件，毫秒级退出；
- Claude Code 自动更新成新版本 → 下次启动时给新二进制打补丁；
- 注意：更新后的**第一个**会话仍运行未打补丁的旧进程（界面是英文），
  **下一个会话**起就是日语。

## 目录

```
themes/asuka.json               EVA-02 配色主题（纯颜色）
statusline/statusline.py        EVA-02 SYNC 状态栏
settings/asuka.fragment.json    spinner 动词 / 横幅 / 主题入口
localize/asuka_patch.py         字符串表补丁工具
localize/asuka-strings.json     英 → 日翻译表（176 条）
install.sh                      一键安装
```

## 自己加词条

编辑 `localize/asuka-strings.json`，加一条：

```json
{ "en": "英文原文（必须和二进制中完整记录一致）", "ja": "日本語" }
```

然后：

```bash
python3 ~/.claude/asuka/asuka_patch.py --check   # 只检查，不写入
python3 ~/.claude/asuka/asuka_patch.py           # 应用
```

`--check` 会打印每条匹配到的记录数和 hash 校验结果；长度超限会直接报错。

修改**已有词条**的译文时，要先还原再打补丁（补丁只认英文原文，已日化的记录它找不到）：

```bash
python3 ~/.claude/asuka/asuka_patch.py --restore
# 改 localize/asuka-strings.json 后重新应用
python3 ~/.claude/asuka/asuka_patch.py
```

## 已知限制

- 少于 4 个字符的字符串通常不是独立记录；能定位到独立记录的（如 ` on`、`ran`）仍可替换。
- 个别通用片段会同时影响所有引用该记录的文案：` to ` → `→`、` on` → `起動`、
  `directory`/`directories` → `ディレクトリ`。改动它们前先跑一遍 `--check` 看看命中数。
- `auto mode`、`don't ask` 这类词也会出现在发给模型的提示词里；本表只替换独立存在的界面标签
  （如 `Don't Ask`），提示词中的长句保持英文。
- 含非 Latin-1 字符的英文记录（如 ` hook…` 里的省略号）在字符串表里是 UTF-16，
  当前工具只搜索 Latin-1 记录，因此这类条目无法替换。
- 每次 Claude Code 大版本更新后，字符串可能变化；缺失的条目会被跳过并在 `--check` 中列出。

## 免责声明

本项目会修改 Claude Code 的可执行文件，并重新做 ad-hoc 签名。它不隶属于 Anthropic，
也不被 Anthropic 支持。请自行承担风险；备份文件请保留。
本仓库**不包含**任何 token、账号或 `settings.json` 里的私人配置。
