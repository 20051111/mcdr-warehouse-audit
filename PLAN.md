# 仓库审计日志插件 — 实施计划（PLAN）

> 对应 SPEC.md v1.0.0-draft ｜ 单文件插件：`warehouse_audit.py`

---

## 1. 架构总览

```
                 ┌──────────────────────── MCDR 插件进程 ────────────────────────┐
                 │                                                                │
 玩家操作箱子 ──►│  ┌──────────────┐   changes    ┌──────────────┐   batch insert │
 (RCON 只读)     │  │  轮询线程     │ ───────────► │  写库线程     │ ────────────► │ ┌──────────┐
                 │  │  Poller      │  Queue(max)  │  DBWriter    │                │ │ SQLite   │
                 │  │              │              │              │ ◄───────────── │ │ (WAL)    │
                 │  │ · 枚举容器    │              │ · executemany│   读连接        │ └──────────┘
                 │  │ · 读 Items    │              │ · 批量提交    │                │      ▲
                 │  │ · 读玩家坐标  │              │              │                │      │ SELECT
                 │  │ · diff 归因   │              └──────────────┘                │      │
                 │  └──────────────┘                                              │ ┌──────────┐
                 │         │ server.rcon_query()（线程安全，含重连）               │ │ CSV 导出  │
                 │         ▼                                                      │ └──────────┘
                 │  ┌──────────────┐   指令     ┌──────────────┐                  │
                 │  │ MCDR 指令     │ ◄───────── │ !!wh ...     │ ◄── 游戏内/控制台 │
                 │  └──────────────┘            └──────────────┘                  │
                 └────────────────────────────────────────────────────────────────┘
```

**关键决策记录：**
1. **只用 `server.rcon_query()`**：MCDR 内置连接线程安全、自动重连、已修复 Minecraft RCON 多包缺陷（MC-87863）。自建 RCON 客户端实测会因流水线发包被服务端断开，弃用。
2. **不解析控制台输出**：`data get` 响应直接经 RCON 套接字返回，无需 `on_info` 捕获（MinecraftDataAPI 模式），也无日志噪音。
3. **未加载区块跳过而非强读**：未加载区块内不可能发生变化；强行 `forceload` 会改变服务端行为，不可接受。
4. **枚举双机制**：小区域 RCON 逐块扫描（实时准）；大区域/区块未加载时解析区域文件（不依赖加载状态）。

---

## 2. 模块划分（单文件内分区）

| 区段 | 内容 | 行数估计 |
| --- | --- | --- |
| A | 元数据与常量 | ~30 |
| B | SNBT 解析器（递归下降，支持带后缀数字/双引号与单引号字符串/嵌套） | ~130 |
| C | 二进制 NBT 解析器 + 区域文件（.mca）区块遍历 | ~140 |
| D | 容器枚举器（RCON 扫描 + 区域文件扫描 + auto 选择） | ~90 |
| E | 轮询线程（读玩家→读容器→diff→归因→入队） | ~130 |
| F | 写库线程（批量 executemany，WAL） | ~60 |
| G | CSV 导出（流式写，utf-8-sig，筛选参数） | ~110 |
| H | 指令处理（help/status/export/rescan/list/on/off） | ~150 |
| I | 插件入口（on_load/on_unload/register，配置装载，世界目录解析） | ~120 |

---

## 3. 实现顺序与检查点

1. **B：SNBT 解析器** ← 全部数据入口，最先写并单测
   - 输入样例（实测格式）：
     - `[{Slot: 0b, id: "minecraft:diamond", Count: 5b}, {Slot: 1b, id: "minecraft:oak_planks", Count: 64b}]`
     - `{z: 9, x: 4, id: "minecraft:chest", y: 100, Items: []}`
     - `[-10146.425246841434d, -63.875d, 2739.505970373987d]`
     - `"minecraft:chest"`
   - 必须处理：无引号键、`b/s/L/f/d` 后缀、嵌套复合标签与列表、空 `{}`/`[]`、字符串内转义引号。
2. **C：区域文件解析器** ← 已在勘查阶段验证可读（r.0.0.mca 实测解析出 74 个方块实体）
   - 只解压与配置区域相交的区块（按区域文件头定位，跳过区域外区块）。
3. **D/E：枚举与轮询**
4. **F：写库线程**
5. **G/H/I：导出、指令、入口**

## 4. 测试计划

### 4.1 离线单元测试（`test_warehouse_audit.py`，不连服务器）
- SNBT 解析：上述全部样例 + 嵌套附魔 NBT + 异常输入（不抛异常）。
- diff 逻辑：构造 before/after 快照，验证存入/取出/替换/清空/双箱半箱各场景的 delta 与记录条数。
- 归因逻辑：同维度/跨维度/半径边界/多人 ambiguous/无人 NULL。
- 区域文件解析：用真实 r.0.0.mca（只读打开）验证能数出仓库 12 箱。
- CSV 导出：造 50 条假数据 → 导出 → 校验表头/BOM/行数/筛选条件。
- RCON 响应解析：`X, Y, Z has the following block data: ...` 前缀匹配、`The target block is not a block entity`、`That position is not loaded` 三分支。
- 指令解析：export 参数解析边界。

### 4.2 在线验证（镜像服，加载后）
1. 复制到 `plugins/warehouse_audit.py` → AutoPluginReloader 10 秒内自动加载 → 查 MCDR.log 无异常、枚举到 12 箱。
2. `!!wh status`（用户可在游戏内执行，或经日志确认状态行）。
3. **端到端**：临时将 bot_mob（假人，原位置 -10146.425,-63.875,2739.506 主世界）传送至仓库旁 → RCON `item replace` 存取物品 → 轮询记录落库 → 校验玩家归因为 bot_mob → 恢复 bot_mob 原位。
4. `!!wh export` 由用户在游戏内触发验证（或离线 harness 验证等价逻辑）。
5. 清理：删除测试产生的临时数据行（或使用独立测试数据库验证后删除）、确认世界无残留改动。

## 5. 风险与对策

| 风险 | 对策 |
| --- | --- |
| 轮询线程持 RCON 锁过久影响其他插件 | 每周期命令数 = 容器数 + 2，默认 14 条，单条约 1ms，总计 <50ms |
| 区域文件解析 CPU 峰值 | 只解压相交区块；按 (path, mtime, size) 缓存；大区域默认不用此法 |
| `save-all flush` 造成 590ms 主线程阻塞 | 默认关闭（`save_before_rescan: false`），仅初始枚举后可选开启 |
| 插件线程异常 | 所有线程主循环包 try/except + 指数退避重试，绝不向上抛 |
| 用户区域坐标理解偏差 | 配置支持多区域；`!!wh list` 可即时核对枚举结果 |

## 6. 交付物清单

- `warehouse_audit_plugin/SPEC.md`
- `warehouse_audit_plugin/PLAN.md`
- `warehouse_audit_plugin/warehouse_audit.py`（部署到镜像服 plugins/）
- `warehouse_audit_plugin/test_warehouse_audit.py`（离线测试）
- `warehouse_audit_plugin/README.md`（使用说明）
