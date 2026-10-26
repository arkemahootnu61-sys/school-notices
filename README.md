# 校园通知雷达

**本人手动点击一次，采集一次。没有定时任务、开机自启或后台采集。**

本机运行的校园通知采集与浏览工具：按「通知公告」类栏目只读抓取公开页面，把通知存进本机 SQLite，用本地网页控制台按日期、来源、关键词浏览、收藏和标记已读。失败或受限会逐来源列出原因，不把「没采到」说成「没有通知」。

默认附带一份**西电（西安电子科技大学）示例来源**，可以直接改成你自己学校的网址；采集、登录、会话保存的域名范围都来自配置，不写死某一所学校。

## 它做什么 / 不做什么

- 只收通知、公告、公示，以及竞赛报名、选拔、培训安排、赛程等；默认排除新闻、宣传报道与获奖喜报。
- 只读 GET/HEAD，不提交表单、不自动登录、不下载附件、不遍历全站。
- **不**自动操作成绩、个人档案、奖助申请等业务系统。
- **不**配置 VPN、代理或绕过校园网/登录权限。
- 每次业务读取前检查同源 `/robots.txt`，明确禁止时跳过并记录原因。

## 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10/11（提供 `.bat` 入口）；macOS / Linux 用同样命令，见下文 |
| Python | 3.10 或更高（`check_python.py` 会检查） |
| Chrome | 可选。仅「仅后台 Chrome / 自动后备 / 学校登录」需要；只用 HTTP 模式不需要 |
| 网络 | 能访问目标校园网站；校外通常需要学校授权的网络通道 |

## 快速开始（Windows）

1. 解压到任意目录（路径含空格、中文都可以，不需要管理员权限）。
2. 双击 **`setup.bat`**：创建项目内 `.venv`、安装依赖、按 `sources.example.json` 生成 `sources.json`。已存在的 `sources.json` 不会被覆盖。
3. 双击 **`run.bat`** 打开本地控制台（浏览器会打开带临时令牌的地址）。
4. 在「来源设置」勾选来源或填写栏目网址 → 点 **保存来源**。
5. 在顶部选择模式与「采集 / 浏览范围」→ 点 **开始采集**。
6. 用「近期通知 / 收藏 / 本次发现·变化」浏览；离开时点 **关闭控制台**。

## 其他系统

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python notices.py init          # 按模板生成 sources.json
.venv/bin/python notices.py serve         # 本地控制台，浏览器会自动打开
```

## 配置来源（可换成任意学校）

`sources.json` 是**你本机的配置**，已被 `.gitignore` 忽略；仓库里只有模板 `sources.example.json`。首次运行会按模板生成，之后随时可在控制台修改。

| 字段 | 说明 |
|---|---|
| `id` | 唯一编号，字母/数字/`_`/`-`，最多 60 字符 |
| `name`、`group` | 显示名与分组，只用于界面 |
| `url` | 栏目入口，必须是普通 `http(s)` 网址 |
| `allowed_hosts` | 允许读取的主机名列表，**不支持通配符** |
| `seed_urls` | 具体列表页入口，可多个 |
| `enabled` | 是否参与采集 |
| `allow_robots_unavailable` | 默认 `false`。本人明确开启后，仅在 robots 连接失败/超时/空响应时继续读取公开通知，并在报告标为「规则未核实」 |
| `allow_http_detail` | 默认 `false`。允许该来源的详情页从 HTTPS 降级到同域 HTTP |
| `list_selector` 等 | 可选 CSS 适配：列表容器、详情链接、条目日期、下一页、标题、日期、正文 |

`session_domains`（可选，根级）声明允许保存登录会话的域名；不写时从各来源 `allowed_hosts` 推导其机构域名。登录窗口和会话快照都只接受这些域名。

`excluded_hosts`（可选，根级）声明永远不作为来源的域名（例如本校新闻网）。微博始终内置排除。模板里预置了示例学校的新闻网，换学校时按需修改。

规则浏览器无法识别的新站点，可先留空让通用识别尝试，再用「采集适配」补 CSS 选择器。**不保证任意网站一次配置就采全。**

## 通知视图

- **近期通知**：默认最近 30 天且未读/未处理的通知；已收录且未变化的默认隐藏，收藏的始终显示。
- **收藏**：个人清单，不受日期范围限制。
- **本次发现 / 变化**：最近一次已提交采集的增量（首次收录、新发现、内容变化）。
- **清理已读**：把已读且未收藏的通知从列表隐藏（保留身份与指纹，之后同一内容不会重新当作新通知；正文真正变化会再次出现）。

相同正文的转载会合并来源与备选链接（摘要长于 60 字符时按正文指纹合并），收藏/已读对合并后的整条通知生效。

## 学校登录（可选，手动）

在控制台点「学校登录」，在弹出的**专用可见 Chrome** 里由本人输入账号密码、处理验证码。完成后回到控制台点「我已完成登录，保存并关闭」。只有明确点击才保存；不自动开始采集，不保证所有站点认证长期有效。

普通 Chrome 的登录状态不会共享；会话存在私有目录，未额外加密，依赖操作系统用户目录权限。

## 安全与隐私

- 控制台只绑定 `127.0.0.1`，用临时令牌验证；不提供对外绑定参数。
- 采集与登录共用一个私有目录锁，一次只跑一个任务。
- 不接收、不保存 Cookie、密码、请求头或登录票据到来源配置；导入含这些字段的配置会被拒绝。
- 私有数据默认在 `%LOCALAPPDATA%\XidianNoticeRadar`（macOS/Linux：`~/.local/share/XidianNoticeRadar`），可用 `--data-dir` 指定。**该目录不要上传或同步到 Git。**
- 控制台页面由本地服务提供，**不要直接双击 `index.html`**（`file://` 打开会提示改用 `run.bat`）。

## 命令行

```bash
python notices.py init                       # 生成默认配置，不覆盖已有
python notices.py serve --port 0 --no-open   # 本地控制台
python notices.py run --only S03 S04         # 单轮采集指定来源
python notices.py run --http --days 60       # 仅 HTTP，最近 60 天
python notices.py run --browser              # 强制专用 Chrome
python notices.py login                      # 终端手动登录流程
python notices.py sources list|add|enable|disable|remove|import
```

全局参数放在子命令之前：`--config sources.json --data-dir <目录>`。
`--days 0` 表示不限日期（不取消请求上限）。默认边界：每入口 2 页、每来源 16 次列表、每来源 25 个详情、全轮 200 页 / 150 详情、每主机至少 1 秒间隔。

## 数据与升级

- 通知库 `notices.sqlite3` 在私有目录；旧版本数据库会在一个事务内升级到当前 `user_version`（当前 4），保留通知、指纹、来源状态与运行结果；高于程序支持的版本会明确报错且不修改文件。
- **从早期版本升级**：1.0.0 之前 `sources.json` 曾被 Git 跟踪，拉取该改动会把它从工作区删除。此时 `init` / `setup.bat` 会先从 Git 历史恢复你原来的配置，再用模板；恢复成功会打印提示。想改用模板请手动删除 `sources.json` 后重新运行 `init`。
- 已保存的登录会话来自另一所学校（或已删除的来源）时，不属于当前配置域名的 Cookie 会被丢弃而不是报错，登录窗口仍可重新登录。
- `output/latest.html` 与带时间戳的 HTML/JSON 是**单轮报告**，不是通知库。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
# 需要本机 Chrome 的隔离浏览器回归：
RUN_BROWSER_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
```

测试使用临时目录、本地 fixture 和虚构 Cookie，不访问真实会话或真实通知历史。Windows 上把 `RUN_BROWSER_TESTS=1` 换成 `set RUN_BROWSER_TESTS=1`。

## 目录结构

```
notices.py            CLI：serve / run / login / sources / init
LICENSE               MIT 许可
check_python.py       setup 时检查 Python 版本
index.html console.js console.css   本地控制台（由本地服务提供）
radar/                配置、解析、传输、存储、采集引擎、任务、报告
sources.example.json  来源模板（首次运行据此生成 sources.json）
sources.json          你本机的来源配置（不入库）
sources.review.json、候选网址清单.md、discovery/   某校的勘察资料，仅供参考，不参与运行
tests/                隔离单元、集成与控制台契约测试
进度报告.md           本机部署的验收记录
output/               单轮报告（不入库）
```

## 已知限制

- 不同学校、不同模板的页面差异很大；通用识别之外仍可能需要 CSS 适配。
- 动态渲染页面需显式使用 Chrome 模式，仍受只读与域名边界限制。
- 需要 POST 查询、验证码或统一认证的列表页不受支持。
- 附件正文不下载；报告只覆盖本轮成功读取的范围，不能证明穷尽全站。

## 许可

MIT License，见 [LICENSE](LICENSE)。可自由使用、修改与再分发（含商业用途），需保留版权与许可声明；软件按“现状”提供，不附带任何担保。

运行时依赖（requests、beautifulsoup4、Playwright、Protego、soupsieve，见 `requirements.txt`）各自遵循其上游许可证，未随本项目再分发。

使用时请自行确认目标网站的 robots.txt、访问条款与学校规定；本项目不授予你访问任何网站或数据的权利。

