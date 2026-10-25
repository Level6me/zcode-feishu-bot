# 更新日志（zcode-feishu-bot）

## v1.0.0（2026-10-25）

首个 zcode 引擎移植版，功能与上游版本 1:1 对齐，并新增：

- **引擎适配层** `zcode_protocol.py`：ZCode Protocol (stdio JSON-RPC) 客户端，
  事件实时翻译为上游流格式，transcript 兼容日志落盘 `~/.zcode/feishu-brain/`
- **计划通道直连**：直接消耗 Z.ai 订阅 / 活动资源包（GLM-5.3 / GLM-5.3-Flash），
  entitlement 推送 + 宿主凭证应答 + 引擎 V4 签名
- **`/plan` 套餐总览**：账户全部套餐、模型额度扣减链（优先级）、有效期倒计时、一键切换
- **`/model` 三档模型**：GLM-5.3 / Flash × 低中高推理档热切换
- **API 免费档后备通道**：内置本地 HTTPS 流式代理（`api_proxy.py`）+ Coding Plan API Key
- **一键部署** `install.sh`：环境探测 → venv/依赖（镜像回退）→ 证书 → 配置 → PM2 守护 + 健康检查
- 其余能力（指令控制台 / 三档授权 / 插件生态 / 定时任务 / 语音多模态 / OTA）与上游一致

上游项目历史变更请参阅其仓库 CHANGELOG。
