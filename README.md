# 分布式网页抓取调度平台

面向分布式网页抓取任务调度、节点协同、请求分配、反爬策略编排、数据校验入库与抓取质量大盘的采集工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集节点 | `worker` | 工作节点 | 节点编号、主机地址、并发槽位数 |
| 请求分配 | `dispatch` | 分配策略 | 策略编号、目标域名、分配算法 |
| 抓取作业 | `crawljob` | 作业定义 | 作业编号、目标站点、起始URL |
| URL过滤 | `urlfilter` | 过滤规则 | 规则编号、包含模式、排除模式 |
| DNS解析 | `dnsresolver` | 解析配置 | 配置编号、域名模式、DNS服务器 |
| 内容提取 | `contentextract` | 提取器 | 提取器编号、内容类型、正文选择器 |
| 存储策略 | `storagestrategy` | 存储方案 | 方案编号、存储类型、连接参数 |
| 渲染任务 | `rendertask` | 渲染作业 | 作业编号、目标URL、浏览器版本 |
| 采集质量 | `qualityreport` | 质量报告 | 报告编号、作业编号、总URL数 |
| 带宽控制 | `bandwidth` | 带宽策略 | 策略编号、节点标识、下载带宽上限 |
| 重试队列 | `retryqueue` | 重试条目 | 条目编号、失败URL、失败原因码 |
| 任务执行侧 | `taskexec` | 作业执行进度 / 结果索引 | 进度编号、作业编号、作业版本、唯一URL集合 |
| 翻页管理 | `pagemarker` | 翻页规则 | 规则编号、目标站点、翻页类型 |
| 增量标记 | `incrementmark` | 增量水位 | 水位编号、目标源、增量字段名 |
| 配置同步 | `configsync` | 同步任务 | 任务编号、源节点、目标节点 |
| 采集API | `scrapeapi` | API定义 | API编号、API名称、请求路径 |
| 数据抽样 | `datasample` | 抽样方案 | 方案编号、数据源、抽样方式 |
| 采集告警 | `alertpolicy` | 告警策略 | 策略编号、监控指标、触发条件 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 失败URL重试与作业执行进度

失败URL的唯一定位键是「作业编号 + 作业版本 + 规范化URL」，规范化逻辑在
`backend/app/services/urlnorm.py`，与作业上的「URL去重策略」保持一致：

- 同一作业版本内，一个规范化URL只有一条重试条目；登记时命中已有条目只合并原因码；
- 重放（网络中断后的补同步、重复提交）按 `幂等键` 幂等，不重复生成条目；
- 重试成功后条目移出队列，失败则按退避次数留在队列，达到上限才进最终失败清单；
- 作业结束（`完成作业` / `停止作业`）之后晚到的重试结果只归档，不会把作业带回执行中；
- 作业 `启动作业` 时版本号递增，上一版本的重试结果仍挂在上一版本的进度上。

任务执行侧接口（`/api/taskexec`）：

| 接口 | 用途 |
| --- | --- |
| `POST /results` | 接收重试结果并回写原作业进度（支持幂等键） |
| `GET /progress` | 作业执行进度，同一作业版本只会有一条 |
| `GET /manifest` | 结果索引清单，落到任务执行侧，可从清单回查进度 |
| `GET /audit?job=&version=` | 按同一URL集合核对计数、明细、版本依据 |
| `GET /reconcile` | 两侧计数核对（队列 / 索引清单 / 进度） |
| `GET /dataset/progress` | 作业执行进度数据集 |
| `GET /dataset/retry-results` | 重试结果数据集（与进度数据集共用同一URL集合） |

重试队列接口（`/api/retryqueue`）新增：

| 接口 | 用途 |
| --- | --- |
| `POST /failures` | 读取失败页面，按唯一URL合并重试条目 |
| `POST /reconnect-sync` | 补同步重放，按幂等键去重 |
| `GET /reasons` | 按原因码检索、统计并定位到具体重试记录 |
| `GET /reconcile` | 队列视角的两侧核对 |

`GET /api/retryqueue` 支持 `reason`、`job`、`version`、`url`、`scope`
（`unconverged` / `converged` / `final_failed`）等筛选参数。

回归用例（标准库 unittest，无需额外安装）：

```bash
cd backend
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

领域：数据工程
子方向：数据采集、爬虫与RPA
