# Midea Auto Cloud

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

美的美居空调 Home Assistant 集成，支持云端控制和 SSE 实时状态推送。

## 功能特性

- ☁️ 云端控制，无需局域网
- ⚡ SSE 实时状态推送，响应迅速
- 🌡️ 支持温度、模式、风速等控制
- 🔧 支持多种美的空调设备（中央空调、挂机、柜机等）

## 安装

### HACS 安装（推荐）

1. 打开 HACS
2. 点击右上角菜单 → 自定义仓库
3. 添加仓库地址：`https://github.com/longjun707/midea_auto_cloud`
4. 类别选择 `Integration`
5. 搜索 `Midea Auto Cloud` 并安装
6. 重启 Home Assistant

### 手动安装

1. 下载本仓库
2. 将 `custom_components/midea_auto_cloud` 复制到你的 Home Assistant 配置目录
3. 重启 Home Assistant

## 配置

1. 进入 Home Assistant → 设置 → 设备与服务
2. 点击添加集成
3. 搜索 `Midea Auto Cloud`
4. 输入美的美居账号密码完成配置

## 支持的设备

- 中央空调 (T0x21)
- 挂机/柜机 (T0xAC)
- 其他美的智能空调设备

## 致谢

- [midea-meiju-codec](https://github.com/sususweet/midea-meiju-codec)

## License

MIT License
