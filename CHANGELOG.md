# Changelog

All notable changes to this project will be documented in this file.

## v1.2.0 - 2026-03-26

- cc内置工具的全面支持
- cli工具调用增加 bracket 风格
- Cherry Studio MCP支持
## v1.1.1 - 2026-03-24

- 单账号多模型路由(与v1.1.0版本一致)
- 之前未提交功能代码，仅发布release
## v1.1.0 - 2026-03-24

- 新增“单账号 + 多模型 + 模型注入”
- 新增映射字段，传入request.model进行模型路由选择
## v1.0.11 - 2026-03-24

- 修改Token计算，直接获取真实的Token数据，新增统一Token解析器。优先：后端返回或流式 chunk 中的真实 usage；其次：analytics run_id usage；最后：估算 fallback（不再固定 1:2 拆分）
## v1.0.1 - 2026-03-24

- 批量导入支持无[] JSON及llm_models自动创建模型组
- 新增Claude llm_models规范化并去除Anthropic前缀
- 新增CHANGELOG/VERSION和发布脚本
## v1.0.0 - 2026-03-24

- Initial release baseline.
