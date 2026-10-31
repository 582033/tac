#!/usr/bin/env bash
# tac Skill 安装脚本
#
# 用法:
#   bash install.sh                # 安装到 ~/.claude/skills/tac/
#   bash install.sh --dir <path>   # 安装到自定义目录
#
# 特性: 幂等（重复执行会先备份旧安装）、不联网、rsync 排除 .venv / data / .git。

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="tac"
TARGET_DIR="${HOME}/.claude/skills/${SKILL_NAME}"

usage() {
    cat <<'EOF'
用法: bash install.sh [--dir <目标目录>]

  --dir <path>   安装到自定义目录（默认 ~/.claude/skills/tac/）
  -h, --help     显示本帮助

示例:
  bash install.sh
  bash install.sh --dir ~/.claude/skills/tac
  bash install.sh --dir .claude/skills/tac        # 项目级 Skill
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dir)
            [ "$#" -ge 2 ] || { echo "错误: --dir 需要一个参数" >&2; exit 1; }
            TARGET_DIR="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "错误: 未知参数 $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [ ! -f "${SRC_DIR}/SKILL.md" ]; then
    echo "错误: ${SRC_DIR} 下未找到 SKILL.md，请在 Skill 根目录运行本脚本。" >&2
    exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
    echo "错误: 需要 rsync，但系统中未找到。" >&2
    exit 1
fi

# 展开 ~ 并把相对路径转成绝对路径
TARGET_DIR="${TARGET_DIR/#\~/${HOME}}"
TARGET_PARENT="$(dirname "${TARGET_DIR}")"
mkdir -p "${TARGET_PARENT}"
TARGET_DIR="$(cd "${TARGET_PARENT}" && pwd)/$(basename "${TARGET_DIR}")"

# 已存在则备份旧目录（幂等：每次安装前都把旧版挪走）
if [ -d "${TARGET_DIR}" ]; then
    STAMP="$(date +%Y%m%d-%H%M%S)"
    BACKUP_DIR="${TARGET_DIR}.bak.${STAMP}"
    echo "检测到已有安装，备份为: ${BACKUP_DIR}"
    mv "${TARGET_DIR}" "${BACKUP_DIR}"
fi

echo "安装: ${SRC_DIR} -> ${TARGET_DIR}"
rsync -a \
    --exclude '.venv' \
    --exclude 'data' \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.DS_Store' \
    "${SRC_DIR}/" "${TARGET_DIR}/"

INSTALLED_FILES="$(find "${TARGET_DIR}" -type f | wc -l | tr -d ' ')"
echo "完成: 共安装 ${INSTALLED_FILES} 个文件到 ${TARGET_DIR}"

cat <<EOF

后续步骤:
  1. 安装依赖（如尚未安装）:
       python3 -m pip install -r ${TARGET_DIR}/requirements.txt
  2. 自检:
       cd ${TARGET_DIR} && python3 scripts/selftest.py
EOF
