# 仓库审计日志插件（WarehouseAuditLog）— 需求规格说明书

> 版本：v1.0.0-draft ｜ 日期：2026-10-18 ｜ 目标平台：Minecraft 1.20.4 Fabric 服务端 + MCDReforged 2.16.0
> 部署目标：`D:\MC\1.20\1.20.1BC端\1.20.4镜像服`（生产镜像/测试服）

---

## 1. 背景与目标

运营方需要一个**仓库容器操作审计系统**：记录谁在什么时候从仓库的箱子/木桶中取出或存入了什么物品、数量多少，以便事后追溯物品流向、排查误拿/偷窃。

本插件以纯 Python 单文件 MCDR 插件形式实现，**不依赖任何第三方插件**（MinecraftDataAPI、Carpet、scarpet 均不需要），只依赖服务端 RCON 通道。

### 1.1 已确认的部署环境事实（2026-10-18 实测）

| 项目 | 实测结论 |
| --- | --- |
| 服务端 | Fabric 1.20.4（`fabric-server.jar`），MCDR 2.16.0 以 Python 3.14 运行 |
| RCON | MCDR config.yml 已启用（127.0.0.1:25575），`server.rcon_query()` 可用且线程安全 |
| 仓库区域（用户指定） | 主世界 `(-10, 99, -10) ~ (10, 101, 10)`，共 1323 方块 |
| 仓库现状 | 区域内已有 **12 个箱子**（`(4..9, 100, 9..10)`，6×2 网格，构成 6 组双箱），区域位于出生点区块保护范围内（命令可直读） |
| 监控容器类型（用户指定） | 仅箱子与木桶：`minecraft:chest`、`minecraft:trapped_chest`（陷阱箱，归入"箱子"）、`minecraft:barrel` |
| 关键命令格式 | `data get block X Y Z Items` → `X, Y, Z has the following block data: [{Slot: 0b, id: "minecraft:diamond", Count: 5b}, ...]` |
| 玩家坐标 | `execute as @a run data get entity @s Pos` → `<玩家> has the following entity data: [x, y, z]`（多名玩家响应在同一字符串内拼接，需正则提取） |
| 未加载区块 | `data get block` 返回 `That position is not loaded`。**未加载区块内不可能发生任何变化**（无 tick、无交互），故跳过读取是安全的 |
| 日志噪音 | `data get` 属于私有命令反馈，**不写入服务端日志**；`save-all` 等广播命令会写入 |
| 世界文件 | `server/world/region/r.*.mca` 可在服务器运行时读取，`block_entities` 位于区块 NBT 根节点 |
| 插件自动重载 | 已安装 AutoPluginReloader，每 10 秒扫描 plugins 目录，新增/修改的插件自动加载/重载 |
| RCON 多包缺陷 | Minecraft RCON 存在 MC-87863（每 TCP 读必须恰好一个包），禁止客户端流水线发包；MCDR 内置 `rcon_query` 已封装 workaround，插件必须复用它而非自建连接 |

---

## 2. 功能需求

### FR-1 容器交互监控
- 持续监控配置区域内所有**箱子/陷阱箱/木桶/潜影盒（无色+16 色共 17 种）**的内容物变化。
- 每个轮询周期读取全部已知容器的 `Items` NBT，与上一周期快照逐槽位比对，产出**变化记录**。

### FR-2 变化记录字段（默认模式：只记取出；不记玩家ID/维度/槽位）
每条记录包含：

| 字段 | 说明 |
| --- | --- |
| 时间戳 | 检测到变化的本地时间（ISO 8601 带时区）+ Unix 时间戳（双格式，便于排序与分析） |
| 物品 ID | `minecraft:diamond` 形式 |
| 数量 | 变化量 `delta`（取出为负）、变化前数量、变化后数量 |
| 坐标 | 容器方块坐标 X/Y/Z、容器类型 |
| 内容物 | 被取出物品若为容器（潜影盒/箱子物品），其内嵌内容概要，如 `packed_ice x640; chest x64` |

> 默认 `record_deposits=false`（存入不记录）、`record_player_id=false`（不记录玩家 ID，
> 也不查询玩家位置）、`record_dimension=false`（不记录维度）、不记录具体槽位
> （DB `slot` 列恒为 -1，仅为兼容历史数据保留）。以上均可在配置中开启。

### FR-2c 取出盒子的 NBT 内容物
- 被取出物品若是容器物品（潜影盒/箱子，含 `tag.BlockEntityTag.Items` 或 1.20.5+ 的
  `components.minecraft:container`），记录其**内容概要**（按物品聚合、数量降序，限 500 字符）。
- 只展开一层（盒中盒不再递归）；普通物品与空盒无内容物。
- 合并记录取首条的内容概要。CSV 末列 `内容物`、`!!wh recent` 末尾展示（超长截断）。

### FR-2b 短时间重复取出合并
- **同一容器、相同物品、同一方向**的多次记录，若在 `merge_window_seconds`（默认 1 秒，
  以组合内首条记录时间为基准）内，**合并为一条，数量相加**。
- 跨槽位取同一物品（如一整箱分开堆放的同物品）同样合并；不同物品/不同容器不合并；
  存入与取出不合并（即使同物品同容器）。
- `merge_window_seconds: 0` 可关闭合并。插件停止时缓冲中的记录会立即写入，不丢失。

### FR-3 玩家归因（可选，默认关闭）
`record_player_id=true` 时启用：
- 每个轮询周期先抓取全服玩家位置（维度 + XYZ），再读容器。
- 容器发生变化时，在**同维度**内查找距容器中心 `attribution_radius`（默认 8 格）以内的玩家，取最近者归因。
- 半径内有 ≥2 名玩家时，记录 `ambiguous=1`（提示人工复核），仍取最近者。
- 无玩家在半径内：`player` 记 NULL（自动化设备如漏斗造成的变更会出现此情况）。

### FR-4 异步队列写入（防卡服）
- 轮询线程只负责"发现变化 → 投递队列"，**绝不直接写数据库**。
- 独立写库线程按 `batch_write_size`（默认 200 条）或 `batch_write_interval`（默认 0.5s）批量 `executemany` 提交 SQLite。
- SQLite 启用 WAL 模式；写库连接仅归写库线程所有。
- 队列超过 `queue_max_size`（默认 10 万）时丢弃最旧记录并告警（保证内存有界）。

### FR-5 CSV 导出
- 指令 `!!wh export` 将记录导出为 CSV（UTF-8 **带 BOM**，Excel 直接双击打开不乱码）。
- 支持筛选：玩家、时间范围、物品、指定容器坐标、行数上限、自定义输出路径。
- 默认输出到插件数据目录 `export/warehouse_audit_YYYYMMDD_HHMMSS.csv`。
- 默认模式（不记玩家ID）CSV 列：`时间,维度,X,Y,Z,容器类型,槽位,物品ID,变化量,变化前,变化后,方向`；
  开启玩家记录后追加 `玩家,玩家距离,多人可疑` 三列。

### FR-8 游戏内查看最近记录
- 指令 `!!wh recent [条数]` 在游戏内直接查看最近 N 条记录（默认 10，最多 100，最新在上）。

### FR-6 容器发现（枚举）
两种机制，`enumeration_method: auto` 自动选择：
1. **RCON 扫描**（默认优先，区域 ≤ 2 万方块时）：逐块 `data get block X Y Z id`，实时准确，发现 `That position is not loaded` 超过阈值时回退到机制 2 补齐。
2. **区域文件解析**：读取 `world/region/r.*.mca` 的 `block_entities`（二进制 NBT 解析），不依赖区块加载状态，但数据新鲜度取决于世界自动保存（默认 5 分钟）。

触发时机：插件加载时、每 `rescan_interval_seconds`（默认 300s）、`!!wh rescan` 手动触发。

### FR-7 运行管理指令
- `!!wh` / `!!wh help`：帮助
- `!!wh status`：运行状态（容器数、队列深度、数据库行数、上次轮询耗时等）
- `!!wh export [选项]`：导出 CSV（文件开头带 `#` 备注行：统计时间跨度、服务端版本、匹配行数、导出时间）
- `!!wh rescan`：立即重新枚举容器
- `!!wh list`：列出当前已知容器坐标
- `!!wh recent [N]`：游戏内查看最近 N 条记录（默认 10，最多 100）
- `!!wh on` / `!!wh off`：暂停/恢复轮询（不停止写库）

### FR-9 监控区域手动配置（游戏内指令）
- `!!wh region`：查看当前监控区域列表
- `!!wh region set <名称> <维度> <x1> <y1> <z1> <x2> <y2> <z2>`：添加/更新区域
  （同名更新、异名新增；坐标自动排序；维度支持 overworld/the_nether/the_end 及中文别名；
  立即生效——写入配置并触发重新枚举）
- `!!wh region del <名称>` / `!!wh region clear`：删除/清空区域
- 以上需 helper（2）级及以上权限

### FR-11 崩溃/回档场景的数据安全
- 服务端关闭时轮询暂停、合并缓冲立即写出、恢复后自动继续；
- 数据库在 MCDR 实例目录（不在世界目录），世界回档不影响审计数据；
- 服务器重启后重新基线，回档造成的内容突变不产生幽灵记录；
- MCDR 优雅退出时排空写入队列；强杀时已提交记录不丢（WAL），最多丢约 0.5 秒未写记录。
- MCDReforged >= 2.1.0（实测 2.16.0）
- Minecraft 1.13~1.20.4 全兼容实测；1.20.5+ 兼容（物品组件化格式自适应解析）
- 任意 Java 版服务端（Vanilla/Paper/Spigot/Fabric/Forge），需启用 RCON；基岩版不适用
- 解析器双格式：`{Slot/id/Count,tag}`（≤1.20.4）与 `{slot/item/count,components}`（≥1.20.5）；
  嵌套内容物 `tag.BlockEntityTag.Items` 与 `components.minecraft:container`
- 区域文件解析兼容根级 `block_entities` 与 `sections[].block_entities`

---

## 3. 非功能需求

| 编号 | 要求 |
| --- | --- |
| NFR-1 | **不卡服**：轮询全程只做 RCON 只读命令（`data get` 单次服务端开销 <0.1ms）；写库在独立线程；周期任务单次服务端主线程占用 < 5ms |
| NFR-2 | **零第三方依赖**：只用 Python 标准库 + MCDR API（自研 SNBT 解析器与二进制 NBT 解析器） |
| NFR-3 | **鲁棒性**：RCON 掉线自动等待重连；单条命令解析失败不影响整周期；未加载区块跳过；插件异常不崩溃 MCDR |
| NFR-4 | **可配置**：区域（支持多区域）、容器类型、轮询间隔、归因半径、数据库/导出路径均可在配置文件中调整，改配置重启插件生效 |
| NFR-5 | **安全**：`export/rescan/list/on/off` 需 helper 级（2）及以上权限；插件不向世界写入任何数据 |

---

## 4. 数据设计

### 4.1 SQLite（`config/warehouse_audit/warehouse_audit.db`，WAL 模式）

```sql
CREATE TABLE IF NOT EXISTS container_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,             -- ISO8601 本地时间
    ts_unix         REAL    NOT NULL,             -- Unix 时间戳
    region          TEXT    NOT NULL DEFAULT '',  -- 区域名
    dimension       TEXT    NOT NULL,             -- minecraft:overworld
    x               INTEGER NOT NULL,
    y               INTEGER NOT NULL,
    z               INTEGER NOT NULL,
    block           TEXT    NOT NULL,             -- minecraft:chest
    slot            INTEGER NOT NULL,
    item_id         TEXT    NOT NULL,
    delta           INTEGER NOT NULL,             -- +存入 / -取出
    before_count    INTEGER NOT NULL,
    after_count     INTEGER NOT NULL,
    player          TEXT,                         -- NULL = 无法归因
    player_distance REAL,                         -- 归因玩家距容器中心距离
    ambiguous       INTEGER NOT NULL DEFAULT 0    -- 1 = 半径内多人
);
-- 索引：ts_unix / player / (x,y,z) / item_id
```

### 4.2 CSV 列（表头中文，utf-8-sig）

默认模式（不记玩家/维度/槽位）：

```
时间,X,Y,Z,容器类型,物品ID,变化量,变化前,变化后,方向,内容物
```

`内容物`：被取出物品为容器（潜影盒/箱子物品）时的内嵌内容概要，如
`packed_ice x640; white_stained_glass x576; chest x64`；非容器物品为空。
开启 `record_player_id` / `record_dimension` 后 CSV 追加对应列。

开启玩家记录后（`record_player_id=true`）追加三列：

```
时间,玩家,维度,X,Y,Z,容器类型,槽位,物品ID,变化量,变化前,变化后,方向,玩家距离,多人可疑
```

`方向` = 存入/取出（默认只记取出，故只有"取出"）。

### 4.3 配置文件（`config/warehouse_audit/warehouse_audit.json`，首次运行自动生成）

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
  "record_player_id": false,
  "record_dimension": false,
  "merge_window_seconds": 1.0
}
```

| 键 | 说明 |
| --- | --- |
| `record_deposits` | `false`=只记录取出（**默认**）；`true`=存入也记录 |
| `record_player_id` | `false`=不记录玩家 ID 且不查询玩家位置（**默认**）；`true`=启用最近玩家归因 |
| `record_dimension` | `false`=不记录维度（**默认**）；`true`=CSV 增加"维度"列 |
| `merge_window_seconds` | 同一容器相同物品的多次取出在此时长内合并为一条、数量相加（**默认 1.0**，0=不合并） |

---

## 5. 已知限制（轮询机制的固有权衡，需向用户明示）

1. **轮询粒度**：默认 1s。同一周期内"取出又放回"的净零变化不会被记录。
2. **默认只记取出**：`record_deposits=false` 时存入不记录；物品被替换时旧物品的取出仍会记录。
3. **默认不记玩家、不记槽位**：开启玩家记录后，归属是推断而非事实（记录距变化容器最近的同维度玩家）；
   合并记录的 `before_count`/`after_count` 取首次/末次受影响槽位的值，跨槽位合并时二者与 delta 不构成算术关系。
4. **合并窗口**：以组合内首条记录时间为基准 1 秒；持续的每 0.9 秒取一次会按 1 秒粒度分组。
5. **新容器发现延迟**：RCON 扫描实时；区域文件解析最多滞后一个自动保存周期（约 5 分钟）。
6. **双箱**按两个半箱分别记录（坐标不同），属正常现象。
7. 容器被破坏时，其内容物以"全部取出"形式记录。

---

## 6. 验收标准（全部达成，2026-10-18 镜像服实测）

1. ✅ 插件在镜像服加载无报错，枚举到 15 个容器（12 箱 + 1 木桶 + 2 潜影盒）。
2. ✅ 玩家取出物品 ≤2 秒内落库，字段完整；**存入不产生记录**（实测验证）。
3. ✅ 潜影盒（含空盒无 `Items` 标签场景）监控正常（实测验证）。
4. ✅ `!!wh export` 生成的 CSV 可被 Excel 直接打开，筛选参数生效；`!!wh recent` 游戏内可查最近记录。
5. ✅ 轮询期间服务端无卡服（每周期仅十余条只读 RCON 命令，`data get` 不写服务端日志）。
6. ✅ 断开 RCON/停止服务器时插件不崩溃，恢复后自动继续。
