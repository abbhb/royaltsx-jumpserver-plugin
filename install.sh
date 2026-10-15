#!/usr/bin/env bash
# Install the JumpServer dynamic folder for Royal TSX.
#
#   ./install.sh                 # install + generate the .rdfe
#   ./install.sh --force         # overwrite an existing config.json
#
# Everything it writes lives under ~/.config/royaltsx-jumpserver/ plus the
# generated dist/JumpServer.rdfe inside this repo.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="$HOME/.config/royaltsx-jumpserver"
CONFIG="$TARGET_DIR/config.json"
RDFE="$REPO_DIR/dist/JumpServer.rdfe"
FORCE=0

for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
    echo "找不到 python3。macOS 自带的 /usr/bin/python3 应该存在，请检查 PATH。" >&2
    exit 1
fi

mkdir -p "$TARGET_DIR"
chmod 700 "$TARGET_DIR"
install -m 755 "$REPO_DIR/src/jms_royaltsx.py" "$TARGET_DIR/jms_royaltsx.py"
echo "已安装脚本 -> $TARGET_DIR/jms_royaltsx.py"

install -m 755 "$REPO_DIR/src/jms_terminal.sh" "$TARGET_DIR/jms_terminal.sh"
echo "已安装终端包装脚本 -> $TARGET_DIR/jms_terminal.sh"

if [ -f "$CONFIG" ] && [ "$FORCE" -eq 0 ]; then
    echo "配置已存在，保持不变 -> $CONFIG"
    echo "（要覆盖请加 --force）"
else
    cp "$REPO_DIR/config.example.json" "$CONFIG"
    chmod 600 "$CONFIG"
    echo "已生成配置模板 -> $CONFIG"
fi

mkdir -p "$REPO_DIR/dist"
"$PY" "$TARGET_DIR/jms_royaltsx.py" --emit-rdfe "$RDFE" 2>/dev/null

cat <<EOF

安装完成。接下来：

1. 打开 Royal TSX，导入动态文件夹：
     $RDFE

2. 选中导入进来的 "JumpServer" 文件夹，在右边的 Credentials 页选
   "Specify username and password"，填上你的 JumpServer 用户名和密码。
   密码加密存在文档里，不用写进任何配置文件。

3. 右键该文件夹选 Reload，资产树就出来了。

如果想先在终端里验证 API 能不能通（可选）：
     $PY $TARGET_DIR/jms_royaltsx.py --check
   注意：在终端里跑看不到 Royal TSX 里的凭据，认证那一步会失败。
   可以临时把密码放到钥匙串里再跑：
     security add-generic-password -s royaltsx-jumpserver -a <用户名> -w '<密码>' -U
   并把 config.json 的 "password_source" 临时改成 "auto"。

配置文件（只有要改分组方式等选项时才需要动）：
     $CONFIG

EOF
