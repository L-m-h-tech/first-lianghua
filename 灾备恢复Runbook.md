# monitor.db 备份与灾备恢复 Runbook（G19，第46轮落地）

> 适用：`data/monitor.db`（SQLite，WAL 模式，含 quotes/minute_bars/signals/signal_outcomes/news/options/
> paper_* /backtest_runs 等全部家当）。本 Runbook 说明**怎么自动备份、怎么校验、库坏了怎么恢复、没有备份时怎么兜底**。
> 配套工具：根模块 `db_backup.py`（纯标准库，只读源库、只写 `backup/`，不接主循环、不改综合分）。

---

## 1. 备份机制（已实现）

- **在线热备，不用停程序**：`db_backup.py` 用 SQLite 官方 Online Backup API（源库以只读 URI 打开），
  即使 `main.py` 常驻写库，也能得到一致性快照，对 WAL 安全、不持长锁。约 350MB 库实测 10 秒内。
- **备份位置与命名**：`backup/monitor_YYYYMMDD-HHMMSS.db`，每份配一个同名 `.db.json` sidecar
  （记录源/副本大小、quick_check 结果、各表行数、程序版本、时间）。
- **滚动保留**：默认保留最近 **30 份**，超出按时间戳删最旧（sidecar 一并删）；只认本工具命名前缀，
  **绝不误删 `backup/` 里其它文件**。
- **备份后自检**：对**副本**跑 `PRAGMA quick_check`，不通过立即删除坏副本并报错——**不留"看起来有、实际坏"的假备份**。
- 备份二进制（`backup/*.db`、`*.db.json`）已在 `.gitignore` 忽略，不入库；任务计划 XML 模板入库。

## 2. 日常手动操作（PowerShell，项目根目录）

```powershell
D:\Python\python.exe db_backup.py --once                 # 立即备份一次（默认保留30份）
D:\Python\python.exe db_backup.py --once --keep 60       # 改保留份数
D:\Python\python.exe db_backup.py --list                 # 列出现有备份（大小/副本qc/版本）
D:\Python\python.exe db_backup.py --verify               # 对所有备份跑 quick_check（异常会返回退出码1）
D:\Python\python.exe db_backup.py --verify --latest-n 3  # 只校验最新3份
D:\Python\python.exe db_backup.py --version              # 打印程序版本（main.py 也支持 main.py --version）
```

建议节奏：**每个交易日收盘后一次**（任务计划自动做，见第 3 节）；大版本升级、大批量回填分钟库前后手动各来一次。

## 3. 开机自启 / 每日定时（只生成、不擅自改系统）

工具只生成文件，**是否注册到 Windows 由你决定**，二选一：

### 3.1 生成内层看门狗与任务模板

```powershell
D:\Python\python.exe db_backup.py --emit-bat --emit-task-xml --daily 16:30
```

- 生成 `run_backup.bat`（切到项目目录 → 调 `db_backup.py --once`，失败 pause，可双击手动跑）；
- 生成 `backup/futures_monitor_db_backup_task.xml`（**每日 16:30 + 用户登录时**各备份一次，
  最小权限、错过可补跑 `StartWhenAvailable`、30 分钟超时、忽略重复实例）。

### 3.2 导入任务计划（任选一种）

- **图形界面**：`Win+R` → `taskschd.msc` → 右侧「导入任务…」→ 选
  `backup/futures_monitor_db_backup_task.xml` → 确定。
- **命令行**（PowerShell，一行）：
  ```powershell
  schtasks /Create /TN "FuturesMonitor_DbBackup" /XML "backup\futures_monitor_db_backup_task.xml" /F
  ```
  卸载：`schtasks /Delete /TN "FuturesMonitor_DbBackup" /F`。
- 不想用任务计划：把 `run_backup.bat` 的快捷方式放进
  `Win+R` → `shell:startup` 打开的启动文件夹，即"开机登录后补一次"（无每日定时）。

### 3.3 期限结构缓存增量补K线（G22续④，第78轮并入本 Runbook）

term_history 的逐合约缓存"有缓存即跳过"，已缓存合约的末根会停在最初下载日。第77轮起提供增量补K线：

  D:\Python\python.exe term_history.py --topup                 # 全品种近6个月：无缓存下载/仍挂牌且末根落后10天重拉合并
  D:\Python\python.exe term_history.py --topup --codes 螺纹钢,铜 --months-back 3   # 只补指定品种/缩短窗口

- 规则（纯函数 topup_decide）：无缓存→new；**仍挂牌**（合约月≥上个日历月）且末根落后 `--stale-days`（默认10天）→stale 重拉（INSERT OR REPLACE 幂等合并）；已退市合约不补。
- 已退市/未挂牌合约被新浪拒绝会软降级登记 error 留待下次重试，不阻断其余合约。
- 任务模板：`backup/futures_monitor_term_topup_task.xml`（每日 18:00 盘后、需网络；**只生成不自动注册**），导入任选其一：
  1) 图形界面：taskschd.msc → 导入任务 → 选 `backup/futures_monitor_term_topup_task.xml` → 确定。
  2) 命令行：`schtasks /Create /TN "FuturesMonitor_TermTopup" /XML "backup\futures_monitor_term_topup_task.xml" /F`；卸载：`schtasks /Delete /TN "FuturesMonitor_TermTopup" /F`。
- 建议排在每日备份（16:30）之后、盘后复盘前执行；与 main 常驻采集互不影响（本命令只读 cache/term_history.db 单表 upsert + 新浪日K）。

### 3.4 影子信号每日追踪（G7/G25续，第83轮并入本 Runbook）

第82轮拍板（路径A）：把"长窗动量排序 xsmom252 基线 / tsmom252 单因子 / 剔除能化对照"三个影子信号挂离线每日追踪——**只记录、不进综合分**，前向积累真正的样本外证据（回测再漂亮也可能是口径/时段幸存者偏差，影子是唯一干净的证据链）。

  D:\Python\python.exe tools\shadow_track.py --daily    # 全链：term top-up → 长面板重建 → 记录当日信号 → 到期评估 → 报告
  D:\Python\python.exe tools\shadow_track.py --report-only   # 只看报告

- 信号登记表（改动=重置影子并在 shadow_meta 登记原因）：xsmom252_baseline / tsmom252_factor / xsmom252_ex_energy（对照列）。
- 产出：`cache/shadow_signals.db`（信号库）+ `reports/shadow_track.txt/.json`（绩效+当日多空腿快照）。
- 启动日守卫：首次运行写入 shadow_start_date，**生产环境永不回填历史**；第一个信号 H=20 交易日后到期（约1个月）。
- 任务模板：`backup/futures_monitor_shadow_task.xml`（每日 18:15、需网络；**只生成不自动注册**）：
  `schtasks /Create /TN "FuturesMonitor_ShadowTrack" /XML "backuputures_monitor_shadow_task.xml" /F`
- 注意：--daily 已内置 top-up 与长面板重建；若同时注册了 §3.3 的 TermTopup 任务（18:00），18:15 的影子任务会重复一次 top-up（幂等无害、多花1-2分钟）。

## 4. 库损坏 / 误操作后的恢复

### 4.1 先识别症状

- 日志/程序报 `database disk image is malformed`、`database disk I/O error`、表查询报错、
  `quick_check` 非 `ok`；或分钟库/信号明显缺失、写入持续失败。
- **先别慌、别反复重启覆盖**：第一时间再手动 `--once` 备份一份"损坏现场"，便于事后排查。

### 4.2 用备份恢复（推荐，安全不丢现场）

```powershell
D:\Python\python.exe db_backup.py --list                      # 先挑一份 qc=ok、时间合适的备份
D:\Python\python.exe db_backup.py --verify --latest-n 5       # 确认最近几份是好的
# 交互式（会要求输入 yes）：
D:\Python\python.exe db_backup.py --restore backup\monitor_20260903-173414.db
# 脚本/无人值守（跳过确认）：
D:\Python\python.exe db_backup.py --restore backup\monitor_20260903-173414.db --yes
```

恢复的安全策略：
1. 先对**待恢复备份**跑 quick_check，坏备份直接拒绝；
2. 现有 `monitor.db`（以及 `-wal/-shm`）**先改名留存**为
   `monitor.db.before_restore_YYYYMMDD-HHMMSS*`，**绝不直接覆盖丢失现场**（确认无误后可手动删这些 `.before_restore_*`）；
3. 用 Online Backup 把备份一致性写回新库，再对新库 quick_check，非 ok 会报错。
4. 恢复后启动 `main.py` 前，先 `--list/--verify` 或用 DB 工具抽查关键表行数（sidecar 里有备份时的行数可对照）。

### 4.3 没有可用备份时的兜底（降级，尽量少丢）

1. **文本产物兜底**：`reports/` 下 latest_report/daily_review/各研究 sidecar、`logs/monitor.log` 是纯文本，
   不依赖 db，历史结论/复盘仍在；
2. **导出抢救**：若库能部分打开，先 `D:\Python\python.exe tools/db_archive.py` 尝试按年导出可读部分，
   或用 sqlite3 `.recover`（CLI）尽量 dump 出未损坏页到新库；
3. **重建可再生产的数据**：quotes/minute_bars 可由常驻采集与新浪/东财滚动接口逐步回填
   （见总纲 G15），signals/paper 等过程数据无法回填的，以 reports 文本为准补记；
4. 把损坏库文件**单独拷走留证**，不要直接删。

## 5. 备份的异地保管（防整块盘损坏）

- `backup/` 和源库在同一块盘，**防误删但不防磁盘物理损坏**。定期（如每周）把最新 1~2 份 `.db`
  拷到 U 盘 / 网盘 / 另一块盘；350MB 量级成本极低。
- 30 份约占 30×350MB≈10GB，磁盘紧张就调小 `--keep`，但**至少保留最近 5 个交易日 + 每周末一份**。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| `源库不存在` | 确认在项目根目录运行，或用 `--src 绝对路径` 指定 |
| 备份副本 quick_check 非 ok | 工具会自动删坏副本并报错；检查磁盘空间/健康，重试；连续失败先停 main 排查磁盘 |
| 任务计划没跑 | `taskschd.msc` 看「上次运行结果」；确认 XML 里 python 路径、工作目录是本机实际路径（重生成即可） |
| 想换备份盘 | `--dir D:\futures_backup` 指定其它目录，任务 XML/bat 里相应改调用参数 |
| 备份时 main 在写会不会不一致 | 不会，Online Backup API 保证事务一致性快照（WAL 安全） |

---

**边界声明**：`db_backup.py` 只做备份/校验/恢复与自启文件导出，**不自动注册系统任务、不删除任何非本工具命名的文件、
恢复时不覆盖而是留存现有库**；不接 main 主循环、不改综合分与任何生产口径，默认行为完全等价旧版（main 仅新增
`--version` 只读开关，不传无任何变化）。
