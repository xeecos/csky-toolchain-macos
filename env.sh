#!/usr/bin/env bash
# 用法: source ./env.sh —— 把 csky-elfabiv2 工具链加入 PATH
# 默认指向本仓库内的 toolchain/（由 ./build-toolchain.sh 生成）；
# 想用别处的工具链: CSKY_ROOT=$HOME/csky-toolchain source ./env.sh
_env_src="${BASH_SOURCE[0]:-}"
[ -n "${_env_src}" ] || _env_src="${(%):-%x}"   # zsh 无 BASH_SOURCE，用 %x 取本文件路径
_env_dir="$(cd "$(dirname "${_env_src}")" && pwd)"

export CSKY_ROOT="${CSKY_ROOT:-$_env_dir/toolchain}"
export PATH="$CSKY_ROOT/install/bin:$PATH"
echo "[env] PATH += $CSKY_ROOT/install/bin"
echo "[env] $(csky-elfabiv2-gcc --version 2>/dev/null | head -1 || echo '工具链尚未构建')"
