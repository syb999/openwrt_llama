include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-llama
PKG_VERSION:=3.0.1
PKG_RELEASE:=1
# ⚠ luci.mk 会忽略 PKG_RELEASE（它按 VERSION:=$(PKG_VERSION) 打 ipk），
#   所以每次改代码必须递增 PKG_VERSION 的最后一位（2.0.9 → 2.0.10 …），
#   否则 opkg 认为版本没变，装完还是旧文件。
PKG_LICENSE:=MIT
PKG_MAINTAINER:=local

LUCI_TITLE:=LuCI app for local llama.cpp inference
LUCI_DESCRIPTION:=llama-server (GGUF, pure CPU) plus a login-free page at /llama.htm \
 with a C agent: 11 plugins (math / curl / jq / doc / sys / net / owrt / music / batch / table / sysmon)
LUCI_DEPENDS:=+luci-base +zlib +jq-full +curl +python3-light +desub-rk3566

# 包里带 aarch64 二进制（C agent），不能标 all
LUCI_PKGARCH:=aarch64_cortex-a53

# src/ 目录存在时 luci.mk 会自动做：
#   Build/Prepare  → 把 luasrc htdocs root src 复制进 PKG_BUILD_DIR
#   Build/Compile  → make -C src clean compile
#   install        → make -C src install DESTDIR=$(PKG_INSTALL_DIR)
# 所以这里只要把交叉编译参数传进去。
#
#   LDFLAGS=            不能带 -static（musl 静态链接后 dlopen 会失效）
#   -D_FORTIFY_SOURCE=0 OpenWrt 的 musl 没有 __snprintf_chk 这类符号，
#                       不关掉 dlopen 时会报 symbol not found
MAKE_FLAGS += \
	CC="$(TARGET_CC)" \
	CFLAGS="$(TARGET_CFLAGS) -I. -D_FORTIFY_SOURCE=0" \
	LDFLAGS=""

include $(TOPDIR)/feeds/luci/luci.mk

define Package/luci-app-llama/conffiles
/etc/config/llama
endef

# call BuildPackage - OpenWrt buildroot signature
