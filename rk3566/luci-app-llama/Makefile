include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-llama
PKG_VERSION:=1.1.0
PKG_RELEASE:=1
PKG_LICENSE:=MIT
PKG_MAINTAINER:=local

LUCI_TITLE:=LuCI app for local llama.cpp inference
LUCI_DESCRIPTION:=Chat UI for llama-server (GGUF models) plus a login-free page at /llama.htm, with an optional agent mode (tool calling: list/du/read/write/pptx/calc/shell)
LUCI_DEPENDS:=+luci-base +python3-light
LUCI_PKGARCH:=all

include $(TOPDIR)/feeds/luci/luci.mk

# call BuildPackage - OpenWrt buildroot signature
