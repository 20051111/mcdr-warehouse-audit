# 仓库审计日志插件 (WarehouseAuditLog)

监控 Minecraft 服务端指定坐标范围内仓库容器（箱子/木桶/潜影盒）的**取出**事件，
记录 **时间戳、物品 ID、数量、坐标** 到 SQLite 数据库，
异步队列批量写入（不卡服），并支持 `!!wh export` 导出 CSV、`!!wh recent` 游戏内查看。

**默认行为**：只记取出（存入不记录）、不记玩家 ID、不记槽位；
1 秒内同一容器相同物品的多次取出合并为一条、数量相加。

- 平台：MCDReforged ≥ 2.1.0（已在 2.16.0 + Fabric 1.20.4 实测）
- 单文件插件，零第三方依赖（自带 SNBT / 二进制 NBT 解析器）
- 数据来源：服务端 RCON 只读命令 + 世界区域文件枚举

## 部署

```powershell
copy warehouse_audit.py <MCDR实例目录>\plugins\
```

MCDR 会自动加载（若安装了 AutoPluginReloader，约 10 秒内生效；否则重启 MCDR 或用 `!!MCDR plugin load warehouse_audit.py`）。

**前置条件**：MCDR `config.yml` 中 `rcon.enable: true`（本服已满足）。

## 配置

首次运行后生成 `config/warehouse_audit/warehouse_audit.json`：

```json
{
  "enabled": true,
  "regions": [
    {"name": "主仓库", "dimension": "minecraft:overworld", "from": [-10, 99, -10], "to": [10, 101, 10]}
  ],
  "container_blocks": [
    "minecraft:chest", "minecraft:trapped_chest", "minecraft:barrel",
    "minecraft:shulker_box",
    "minecraft:white_shulker_box", "minecraft:orange_shulker_box",
    "minecraft:magenta_shulker_box", "minecraft:light_blue_shulker_box",
    "minecraft:yellow_shulker_box", "minecraft:lime_shulker_box",
    "minecraft:pink_shulker_box", "minecraft:gray_shulker_box",
    "minecraft:light_gray_shulker_box", "minecraft:cyan_shulker_box",
    "minecraft:purple_shulker_box", "minecraft:blue_shulker_box",
    "minecraft:brown_shulker_box", "minecraft:green_shulker_box",
    "minecraft:red_shulker_box", "minecraft:black_shulker_box"
  ],
  "poll_interval_seconds": 1.0,
  "rescan_interval_seconds": 300,
  "enumeration_method": "auto",
  "rcon_scan_max_blocks": 10000,
  "save_before_rescan": false,
  "attribution_radius": 8.0,
  "world_directory": "",
  "database_file": "warehouse_audit.db",
  "export_directory": "export",
  "queue_max_size": 100000,
  "batch_write_size": 200,
  "batch_write_interval_seconds": 0.5,
  "prune_after_misses": 600,
  "record_deposits": false,
  "record_player_id": true,
  "record_dimension": false,
  "anonymous_mode": false,
  "lock_containers": true,
  "merge_window_seconds": 1.0
}
```

| 键 | 说明 |
| --- | --- |
| `regions` | 监控区域列表，支持多区域；`dimension` 为 `minecraft:overworld` / `minecraft:the_nether` / `minecraft:the_end`；`from`/`to` 为对角坐标（顺序任意） |
| `container_blocks` | 监控的容器方块 ID（默认含箱子/陷阱箱/木桶/潜影盒 17 色） |
| `poll_interval_seconds` | 轮询间隔；间隔内"取出又放回"的净零变化不会记录；单周期超时自动降频（不卡服） |
| `enumeration_method` | `auto`（小区域 RCON 逐块扫描，大区域解析区域文件）/ `rcon_scan` / `region_file` |
| `attribution_radius` | 玩家归因半径（格）；仅 `record_player_id=true` 时生效 |
| `world_directory` | 世界目录（`world` 文件夹），留空自动推断 |
| `prune_after_misses` | 容器连续 N 次读取不到后从轮询列表移除 |
| `record_deposits` | `false`=只记录取出（默认）；`true`=存入也记录 |
| `record_player_id` | `true`=记录玩家 ID 并归因最近玩家（**默认**）；`false`=不记录也不查询 |
| `record_dimension` | `false`=不记录维度（默认）；`true`=CSV 增加"维度"列 |
| `anonymous_mode` | `false`=显示真实玩家 ID（默认）；`true`=用数字编号（P1/P2...）代替，一一对应但匿名 |
| `lock_containers` | `true`=首次枚举后锁定容器列表，**区域框定后不再自动新增容器坐标（临时容器不统计）**（默认）；`false`=周期性 rescan 自动纳入新容器 |
| `merge_window_seconds` | 同一容器相同物品的多次取出在此时长内合并为一条、数量相加（默认 1.0；0=不合并） |

修改配置后需重载插件：`!!MCDR plugin reload warehouse_audit`（或重启 MCDR）。

## 指令

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `!!wh help` | 所有人 | 帮助 |
| `!!wh status` | 所有人 | 运行状态（容器数/队列/数据库/最近轮询耗时） |
| `!!wh list` | 所有人 | 列出已知容器坐标 |
| `!!wh recent [条数]` | 所有人 | 查看最近 N 条记录（默认 10，最多 100，最新在上） |
| `!!wh export [选项]` | helper+ | 导出 CSV |
| `!!wh region` | helper+ | 查看监控区域 |
| `!!wh region set <名称> <维度> <x1> <y1> <z1> <x2> <y2> <z2>` | helper+ | 添加/更新监控区域（立即生效并写入配置） |
| `!!wh region del <名称>` / `!!wh region clear` | helper+ | 删除/清空监控区域 |
| `!!wh rescan` | helper+ | 立即重新枚举容器 |
| `!!wh on` / `!!wh off` | helper+ | 恢复/暂停轮询 |

`region set` 的维度可用 `overworld` / `the_nether` / `the_end`（或 `主世界`/`地狱`/`末地`），
坐标两角顺序任意、支持负数。例：

```
!!wh region set 主仓库 overworld -10 99 -10 10 101 10
!!wh region set 地狱仓库 nether -200 30 -200 -180 40 -180
```

`!!wh export` 选项：

```
--player 名字          只导出某玩家的记录
--days 天数            只导出最近 N 天
--from / --to 日期     时间范围 (YYYY-MM-DD[ HH:MM:SS])
--item 物品ID          只导出某物品 (如 minecraft:diamond)
--container X,Y,Z      只导出某容器的记录
--limit 行数           最大导出行数 (默认 500000)
--out 路径             自定义输出路径 (含空格需引号)
```

示例：

```
!!wh export --player Steve --days 7 --out D:\audit\week.csv
!!wh export --item minecraft:diamond --days 1
```

导出文件默认位于 `config/warehouse_audit/export/warehouse_audit_YYYYMMDD_HHMMSS.csv`，
UTF-8 带 BOM，Excel 双击直接打开。文件开头带 `#` 备注行（数据工具可用 `comment='#'` 跳过）：

```
# 备注: 统计时间跨度 2026-10-18T16:13:04.596+08:00 ~ 2026-10-18T17:18:25.160+08:00
# 备注: 服务端版本 Minecraft 1.20.4
# 备注: 匹配记录 54 条 (导出上限 500000)
# 备注: 导出时间 2026-10-18T17:47:50+08:00
时间,X,Y,Z,容器类型,物品ID,变化量,变化前,变化后,方向,内容物
```

## 数据格式

默认模式 CSV 列：`时间, X, Y, Z, 容器类型, 物品ID, 变化量, 变化前, 变化后, 方向, 内容物`

- `变化量`：存入为正，取出为负（默认只记取出，故只有负值）
- `方向`：存入 / 取出
- 1 秒内同一容器相同物品的多次取出已合并为一条、数量相加
- `内容物`：被取出物品为容器（潜影盒/箱子物品）时的内嵌内容概要，如
  `packed_ice x640; chest x64`（只展开一层；非容器物品为空）
- 默认**不记录**玩家 ID、维度、槽位；可在配置中分别开启
  （`record_player_id` / `record_dimension`，开启后 CSV 追加对应列）

**数据实时性**：记录实时写入 SQLite 数据库（每 0.5 秒批量提交），
`!!wh export` 只是把数据库转换为 CSV；也可用 DB Browser for SQLite
直接打开 `config/warehouse_audit/warehouse_audit.db` 查看（注意同时存在
`-wal`/`-shm` 附属文件时不要只拷贝 `.db`）。

SQLite 表 `container_changes` 另含 `region`（区域名）、`ts_unix`（Unix 时间戳）、
`slot`（历史数据保留，新记录恒为 -1）、`ambiguous` 字段，可直接 SQL 分析：

```sql
SELECT item_id, SUM(-delta) AS taken FROM container_changes
WHERE ts_unix > strftime('%s','now','-7 days')
GROUP BY item_id ORDER BY taken DESC;
```

## 运行说明

- **插件日志输出到 MCDR 控制台**（MCDR 的插件 logger 不写入 `logs/MCDR.log`），
  在 MCDR 窗口可见加载/枚举/错误信息。
- 轮询使用 MCDR 内置 RCON 连接（线程安全、自动重连），只发只读命令
  （`data get` / `execute`），其输出属于私有命令反馈，**不写服务端日志、不刷屏**。
- 每 5 分钟自动重新枚举一次容器（默认 `rescan_interval_seconds: 300`），
  枚举期间短暂占用 RCON 锁（本仓库区域约 2~3 秒）。
- 修改插件文件后 AutoPluginReloader 会在约 10 秒内自动重载；
  修改配置后需 `!!MCDR plugin reload warehouse_audit` 或重启 MCDR。

## 数据安全（崩溃 / 回档场景）

| 场景 | 行为 |
| --- | --- |
| **服务端崩溃/关闭** | 轮询自动暂停（不再发 RCON 命令、不报错堆栈）；已入队记录正常落库；合并缓冲中最多 1 秒的记录**立即写出**（不会滞留内存）；服务器恢复后自动继续监控并重新基线 |
| **世界回档**（PrimeBackup/QuickBackup 等） | 数据库位于 `config/warehouse_audit/`（MCDR 实例目录），**不在世界目录内**，回档不会丢失或回退审计数据；重启后插件**重新建立基线**，回档造成的容器内容突变不会被误记为"取出"（幽灵记录） |
| **MCDR 优雅退出**（Ctrl+C / 停止） | `on_unload` 停线程并**排空写入队列**后退出，无丢失 |
| **MCDR 被强杀**（任务管理器/断电） | SQLite 已提交记录不丢（WAL + `synchronous=NORMAL`，进程级崩溃安全）；最多丢失队列中约 0.5 秒未写的记录 |
| **运行中拷贝数据库** | WAL 模式最新数据可能在 `-wal` 附属文件里，只拷 `.db` 可能缺最新几行——请连同 `-wal`/`-shm` 一起拷贝，或直接用 `!!wh export` |

已知取舍：重新基线后，崩溃前**不到一个轮询周期（<1s）内**尚未被记录的取出不会补记
（服务端关闭期间不可能发生交互，该窗口仅限崩溃前约 1 秒）。

## 版本适用性

| 组件 | 要求 / 实测 |
| --- | --- |
| MCDReforged | **>= 2.1.0**（实测 2.16.0；用到 `rcon_query` / `load_config_simple` / 指令构建器） |
| Minecraft | **1.13 ~ 1.20.4 全兼容实测**（1.20.4 生产运行）；**1.20.5+ 兼容**（解析器自适应物品组件化格式，见下） |
| 服务端类型 | 任意 Java 版服务端（Vanilla / Paper / Spigot / Fabric / Forge 等），需启用 RCON；基岩版（Nukkit）不适用 |
| Python | MCDR 运行环境自带（实测 3.14，无第三方依赖） |

版本自适应设计：

- **物品 NBT 双格式**：`1.13~1.20.4` 的 `{Slot/id/Count, tag:…}` 与 `1.20.5+` 的
  `{slot/item/count, components:…}` 均可解析（键名大小写/字段名差异已处理）。
- **嵌套容器内容物双格式**：`tag.BlockEntityTag.Items`（≤1.20.4）与
  `components."minecraft:container"` / `"minecraft:bundle_contents"`（≥1.20.5）。
- **区域文件解析**：区块根级 `block_entities`（1.13~1.20.4 实测），并兼容存放在
  `sections[].block_entities` 的格式；该路径仅为枚举兜底，主路径（RCON 扫描）与区块格式无关。
- **命令兼容性**：`data get block`（1.13+）、`execute as @a run data get entity`（1.13+）、
  `save-all flush`（仅 `save_before_rescan=true` 时使用）。
- **1.13 以下不支持**（无 `data` 命令）；升级到更新版本时如 Minecraft 再次改动
  命令输出格式，解析失败会被安全跳过并告警，不会写入错误数据。

## 已知限制

1. **轮询粒度**：同一周期内"取出又放回"的净零变化不记录（默认 1 秒）。
2. **默认只记取出、不记玩家、不记槽位**；需要完整审计时可在配置中开启
   （开启后归因是推断：记录距变化容器最近的同维度玩家）。
3. **合并窗口**：以组合内首条记录时间为基准 1 秒；跨槽位合并时
   `变化前/变化后` 取首次/末次受影响槽位的值，与 `变化量` 不构成算术关系。
4. **内容物只展开一层**：盒中盒不再递归；合并记录取首条的內容概要。
4. **容器发现延迟**：RCON 扫描实时；区域文件解析模式最多滞后一个世界自动保存周期（约 5 分钟）。
5. **双箱**按两个半箱分别记录（坐标不同），属正常现象。
6. 容器被破坏时，内容物以"全部取出"形式记录。
7. 区块未加载时跳过读取（未加载区块内不可能发生变化），不产生记录。

## 文件

- `warehouse_audit.py` — 插件本体
- `test_warehouse_audit.py` — 离线单元测试（53 个用例，无需连接服务器）
- `SPEC.md` / `PLAN.md` — 需求规格与实施计划
