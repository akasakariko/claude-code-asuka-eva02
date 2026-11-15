#!/usr/bin/env bash
# EVA-02 明日香テーマ + 日本語 UI を Claude Code に導入する。
#   bash install.sh              # テーマ・ステータスライン・日本語化を導入
#   bash install.sh --no-patch   # テーマだけ入れて、バイナリは触らない
#   bash install.sh --unpatch    # 日本語化を取り消す（バックアップから復元）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
TARGET="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
MODE="${1:-}"

mkdir -p "$TARGET/themes" "$TARGET/asuka"

cp "$ROOT/themes/asuka.json" "$TARGET/themes/asuka.json"
cp "$ROOT/statusline/statusline.py" "$TARGET/statusline.py"
chmod +x "$TARGET/statusline.py"
cp "$ROOT/localize/asuka_patch.py" "$ROOT/localize/asuka-strings.json" "$TARGET/asuka/"

python3 - "$TARGET" "$ROOT" <<'PY'
import json, os, sys

target, root = sys.argv[1], sys.argv[2]
path = os.path.join(target, "settings.json")

try:
    with open(path, encoding="utf-8") as f:
        settings = json.load(f)
except FileNotFoundError:
    settings = {}

with open(os.path.join(root, "settings", "asuka.fragment.json"), encoding="utf-8") as f:
    frag = json.load(f)

for key in ("theme", "spinnerVerbs", "companyAnnouncements"):
    settings[key] = frag[key]

settings["statusLine"] = {
    "type": "command",
    "command": os.path.join(target, "statusline.py"),
}

hook_cmd = f'python3 "{os.path.join(target, "asuka", "asuka_patch.py")}" --auto'
hooks = settings.setdefault("hooks", {})
starts = hooks.setdefault("SessionStart", [])
already = any(
    "asuka_patch.py" in h.get("command", "")
    for group in starts
    for h in group.get("hooks", [])
)
if not already:
    starts.append({"hooks": [{"type": "command", "command": hook_cmd, "timeout": 120}]})

if os.path.exists(path):
    with open(path, "rb") as f:
        old = f.read()
    with open(path + ".asuka-bak", "wb") as f:
        f.write(old)
with open(path, "w", encoding="utf-8") as f:
    json.dump(settings, f, ensure_ascii=False, indent=2)
    f.write("\n")
print(f"settings: {path}")
PY

if [ "$MODE" = "--unpatch" ]; then
  python3 "$TARGET/asuka/asuka_patch.py" --restore || true
elif [ "$MODE" != "--no-patch" ]; then
  python3 "$TARGET/asuka/asuka_patch.py" --auto || true
fi

echo
echo "EVA-02 弐号機・導入完了"
echo "  theme    : $TARGET/themes/asuka.json  (/theme -> EVA-02 Asuka)"
echo "  status   : $TARGET/statusline.py"
echo "  localize : $TARGET/asuka/  (SessionStart で自動再適用)"
echo "  解除     : bash install.sh --unpatch"
