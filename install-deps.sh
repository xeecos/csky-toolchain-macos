#!/usr/bin/env bash
# 安装 macOS 上构建 csky-elfabiv2 交叉工具链所需的 Homebrew 依赖
set -euo pipefail

PKGS=(gmp mpfr libmpc texinfo gawk make)

installed=$(brew list --formula 2>/dev/null || true)
missing=()
for p in "${PKGS[@]}"; do
  grep -qx "$p" <<<"$installed" || missing+=("$p")
done

if [ ${#missing[@]} -eq 0 ]; then
  echo "[deps] 所有依赖已安装: ${PKGS[*]}"
else
  echo "[deps] 安装缺失依赖: ${missing[*]}"
  brew install "${missing[@]}"
fi

echo "[deps] 完成。brew prefix: $(brew --prefix)"
