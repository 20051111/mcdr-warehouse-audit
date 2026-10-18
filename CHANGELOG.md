# 更新日志

## v1.0.0 (2026-10-18)

首个发布版本。

### 核心能力
- 监控指定坐标范围内的容器（箱子/陷阱箱/木桶 + 17 种潜影盒）交互事件
- 通过 RCON 只读 `data get block` 轮询（1s 间隔），异步队列 + SQLite WAL 批量写入，不卡服
- 记录玩家 ID、时间戳、物品 ID、数量、坐标、内容物，`!!wh export` 导出 CSV
- 容器自动枚举：区域 ≤2 万方块用 RCON 扫描，超出回退区域文件解析（二进制 NBT）

### 数据记录（默认最小化）
- 默认**只记录取出**（`record_deposits=false`），存入不记录
- 默认**不记录玩家 ID**（`record_player_id=false`，也不查询玩家位置）
- 默认**不记录维度**（`record_dimension=false`）、不记录具体槽位
- 1 秒内同容器同物品的多次取出自动合并为一条、数量相加（`merge_window_seconds=1.0`）

### 内容物读取
- 取出潜影盒/箱子物品时读取其内嵌 NBT 内容概要，如 `packed_ice x640; chest x64`
- 兼容 1.13~1.20.4（`tag.BlockEntityTag.Items`）与 1.20.5+（`components.minecraft:container`）

### 指令
- `!!wh` / `help` / `status` / `list` / `recent [N]`
- `!!wh export [--player --days --from --to --item --container --limit]`
- `!!wh region` / `region set <名称> <维度> <x1> <y1> <z1> <x2> <y2> <z2>` / `region del` / `region clear`
- `!!wh rescan` / `on` / `off`

### 导出
- CSV（UTF-8 带 BOM，Excel 友好），文件头带 `#` 备注行：统计时间跨度、服务端版本、匹配行数、导出时间

### 数据安全
- 服务端崩溃/关闭：轮询自动暂停、合并缓冲立即写出、恢复后重新基线
- 世界回档：数据库不在世界目录，回档不影响审计数据；重启后重新基线避免幽灵记录
- MCDR 优雅退出：排空写入队列；强杀：已提交记录不丢（WAL），最多丢约 0.5 秒未写记录

### 兼容性
- MCDReforged >= 2.1.0（实测 2.16.0）
- Minecraft 1.13~1.20.4 实测；1.20.5+ 自适应解析
- 任意 Java 版服务端（Vanilla/Paper/Spigot/Fabric/Forge），需启用 RCON

### 质量
- 92 项单元测试全部通过（解析器 / 合并 / 枚举 / 导出 / 指令分发 / 生命周期 / 迁移）
