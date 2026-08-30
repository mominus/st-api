# 架构与并发设计决策

## 是否改写为 Go

当前不进行全量 Go 重写。网关的主要耗时来自 StackAI/模型网络等待，现有 FastAPI、
asyncio 与 httpx 热路径均为非阻塞 I/O；将协议转换代码改写为 Go 不会缩短上游首
token 时间，反而会在短期内重新引入三套协议状态机、管理后台、计费、账号池和
SQLite 迁移风险。

Go 在单机连接数达到数万、Python CPU profile 明确显示协议解析占主要 CPU，或服务拆分
后只需要一个无状态 SSE 数据面时才有明显收益。届时推荐保留 Python 控制面，逐步增加
Go 数据面，而不是一次性替换整个服务。

## 当前数据路径

1. FastAPI 接收 Anthropic Messages、Chat Completions 或 Responses 请求。
2. `ProtocolBridge` 归一化消息、工具定义、工具选择和工具历史。
3. `GatewayRuntime` 完成 key/model/account 解析，生成 StackAI workflow 输入。
4. 全局 httpx 连接池复用上游连接；同步和 SSE 请求使用独立并发额度。
5. 输出解析器只把本次请求声明的工具升级为结构化调用，再映射回客户端协议。
6. 用量和非关键日志通过有界异步队列批量写入 SQLite。

账号容量预留通过带额度与 inflight 条件的单条原子 `UPDATE` 完成。多个 worker 读取到
相同候选账号时，只有满足条件并成功更新的 worker 获得容量，避免读取后再修改 ORM
对象造成的超额分配。每日额度带 UTC 日期，跨日后的首次选择会用条件更新统一清零并
恢复因昨日额度耗尽的账号，不再依赖人工调用重置接口。

## 扩容优先级

1. **上游账号与连接额度**：先观察 pool wait、429、首 token 和账号 inflight 指标。
2. **流式连接隔离**：为同步请求预留连接，避免长 SSE 占满整个上游池。
3. **减少 SQLite 写放大**：保持异步聚合和低价值日志丢弃；单机 SQLite 使用一个 worker。
4. **横向扩容**：需要多个 API worker/实例时，将状态、限流、日志和账号租约迁移到
   PostgreSQL/Redis；不要让多个高写入 worker 共享 SQLite 文件。
5. **CPU 优化**：profile 证明 JSON/工具解析成为瓶颈后，再考虑 Rust/Go 扩展或独立数据面。

当前 SQLite 状态可以安全协调同一持久卷上的多个进程，但 SQLite 文件不应通过 NFS 在
多主机之间共享。真正的多主机横向部署仍应把控制面状态迁移到 PostgreSQL，并把全局
限流/短租约迁移到 Redis；在完成该迁移前，推荐横向扩展无状态边缘代理，而 st-api
保持单实例、单 worker。项目不会把进程内计数伪装成全局状态。

## 过载与内存边界

- 入口请求、SSE、数据库操作、模型组及单账号均有独立并发限制。
- 上游同步响应和单条流式记录有大小上限，避免异常工作流无限占用内存。
- 流请求先获取专用 stream permit，再竞争共享连接；排队中的流不会占住同步请求连接。
- 只有首 token 前的幂等失败才允许流式重试/账号切换，防止客户端收到重复内容。
- 队列等待有明确超时，过载时快速失败而不是无限堆积协程。

SQLite 的 `busy_timeout`、`synchronous=NORMAL`、WAL checkpoint、内存临时表和页缓存会
应用到每一条池化连接。使用量历史与 API key 统计按批次执行 UPSERT/executemany，避免
每个请求产生 SELECT + UPDATE 的读改写循环。

## 建议压测方法

分别测试纯文本、单工具、多工具并行和工具结果回传四种场景，记录吞吐、p50/p95/p99、
首 token、连接池等待、队列等待、429/5xx、SQLite busy 和内存峰值。只有同一模型、同一
上游账号、同一并发曲线下的 A/B 数据，才能用于判断语言改写收益。
