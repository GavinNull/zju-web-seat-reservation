# GitHub 发布 Workflow

这份 workflow 用于把本项目整理成可以公开上传 GitHub 的干净版本。核心原则是：先确认功能和文档完整，再确认没有本地登录态、数据库、诊断截图、Cookie、Token、账号信息或测试残留进入仓库。

## 0. 发布目标

发布版本应满足这些结果：

- 新用户可以根据 `README.md` 在 Windows 上启动助手。
- GitHub 仓库只包含源码、测试、示例配置、说明文档和 CI 配置。
- 仓库不包含任何个人登录状态、浏览器 profile、SQLite 数据库、截图、Cookie、Token、账号、密码、预约记录或本地缓存。
- GitHub Actions 能在干净环境里安装依赖并通过测试。

## 1. 准备发布目录

建议只在专门的发布目录中整理上传内容，例如：

```powershell
cd "D:\vibe\reservation - github"
```

不要直接从日常运行目录上传，因为日常目录通常包含 `data/`、`.venv/`、`diagnostics/` 和浏览器登录状态。

确认当前 Git 状态：

```powershell
git status --short --branch
```

如果看到大量不确定的新文件，先分类：

- 源码和测试：可以进入发布候选。
- 文档：需要隐私检查后再进入发布候选。
- 本地运行产物：不要上传。

## 2. 必须排除的隐私和本地数据

以下内容不能上传 GitHub：

- `data/`
- `data/assistant.db`
- `data/browser-profile/`
- `data/reservation-profiles/`
- `diagnostics/`
- `output/`
- `.venv/`
- `.pytest_cache/`
- `__pycache__/`
- `*.egg-info/`
- `.env`
- `.env.*`
- `config.json`
- `*.local.json`
- 任何 Cookie、Token、Authorization header、统一身份认证账号、密码、短信截图、预约记录截图。

确认 `.gitignore` 至少包含：

```gitignore
.venv/
data/
diagnostics/
output/
__pycache__/
.pytest_cache/
*.egg-info/
.env
.env.*
config.json
*.local.json
```

运行检查：

```powershell
git status --ignored --short
```

期望：本地运行目录应显示为 `!!` ignored，而不是 `??` untracked。

## 3. 隐私扫描

上传前做一次文本扫描。下面的命令会查找常见敏感词，不代表百分百覆盖，但能抓住大多数明显问题：

```powershell
Select-String -Path .\README.md,.\docs\*.md,.\docs\**\*.md,.\src\**\*.py,.\tests\**\*.py `
  -Pattern "cookie|token|authorization|password|passwd|secret|session|zjuam|账号|密码|短信|Cookie|Token|Authorization" `
  -CaseSensitive:$false
```

处理规则：

- README 中解释“不要上传 Cookie/Token”是允许的。
- 代码中常量名、脱敏函数和测试用假 token 是允许的。
- 真实值、真实截图、真实账号、真实 Cookie、真实 Authorization header 一律删除或替换为示例。

如果文档里包含接口抓包、调试脚本或截图，必须逐个确认：

- `docs/reservation/` 中的调查材料是否含真实响应、真实 seat id、真实 session 信息。
- `docs/reservation/screenshots/` 是否含个人姓名、学号、账号头像、预约记录。
- `docs/HANDOFF.md` 是否含真实调试证据或本机路径。

不确定是否安全时，不上传该文件。

## 4. README 发布前检查

`README.md` 是 GitHub 首页，发布前应覆盖这些内容：

- 项目一句话说明：这是本地 Web 控制台，不是公网服务。
- 合规声明：不绕过统一身份认证、验证码、二次认证、限流或学校规则。
- 快速开始：优先说明双击 `启动助手.bat`。
- 手动安装：Python 版本、创建 `.venv`、`pip install -e ".[test]"`、安装 Playwright Chromium、启动服务。
- 登录流程：必须在助手弹出的浏览器里登录，普通浏览器登录态不会自动同步。
- 新版预约方式：说明默认启用 HTTP 扫描 + 浏览器代理提交；如果检测异常，先重启服务并重新连接账号。
- 自动预约安全条件：系统总开关开启，且任务关闭观察模式，才会提交预约。
- 隐私说明：列出 `data/`、浏览器 profile、数据库、诊断截图不能上传。
- 故障排查：坏 `.venv`、找不到 Python、登录态失效、任务显示检测失败。
- 项目结构：源码、测试、文档、启动器、CI 配置。
- 免责声明：用户需遵守学校和图书馆规则。

README 中不要出现：

- 个人账号、学号、真实姓名。
- 真实预约记录。
- 本机绝对路径作为唯一说明。
- 只有你电脑上才成立的环境描述。

## 5. 依赖和启动器检查

确认 `pyproject.toml` 包含运行期依赖，而不是把运行依赖放在 test extra 里：

```powershell
Get-Content .\pyproject.toml
```

重点依赖：

- `FastAPI`
- `uvicorn[standard]`
- `Jinja2`
- `APScheduler`
- `playwright`
- `Pillow`
- `httpx`
- `pycryptodome`
- `python-multipart`

确认 `启动助手.bat`：

- 会切到项目目录。
- 会创建或修复 `.venv`。
- 会安装 `.[test]`。
- 会安装 Playwright Chromium。
- 会启动 `zju-seat-assistant.exe`。
- 会默认设置 `ZJU_SEAT_HTTP_ENABLED=1`。
- 如果服务已运行，会打开本地控制台。

## 6. 测试和 CI

本地运行：

```powershell
python -m pytest -q
```

如果使用项目虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

当前发布基准应以实际输出为准，不要在文档里长期写死旧数字。每次上传前记录最新结果，例如：

```text
124 passed, 1 warning
```

确认 `.github/workflows/tests.yml` 会在 GitHub Actions 中执行：

```yaml
python -m pip install -e ".[test]"
python -m pytest -q
```

## 7. 选择要上传的文件

推荐上传：

- `.github/workflows/tests.yml`
- `.gitattributes`
- `.gitignore`
- `README.md`
- `pyproject.toml`
- `config.example.json`
- `启动助手.bat`
- `src/`
- `tests/`
- `docs/GITHUB_UPLOAD_CHECKLIST.md`
- `docs/GITHUB_RELEASE_WORKFLOW.md`
- 经过隐私检查的用户文档。

谨慎上传：

- `docs/HANDOFF.md`
- `docs/http-session-mode.md`
- `docs/reservation/`
- `docs/superpowers/`
- 调查脚本、接口验证报告、抓包说明。

不要上传：

- 任何截图，除非已确认没有个人信息。
- `data/`
- `diagnostics/`
- `.venv/`
- `.pytest_cache/`
- `__pycache__/`
- `*.egg-info/`
- `.claude/`
- 本地 agent 过程文件。

## 8. 提交前 Git 检查

查看将被提交的文件：

```powershell
git status --short
git diff --stat
git diff --check
```

查看已跟踪文件：

```powershell
git ls-files
```

如果发现敏感文件已经被跟踪，不能只依赖 `.gitignore`。需要先从索引移除：

```powershell
git rm --cached <path>
```

然后重新检查：

```powershell
git status --short
git ls-files
```

## 9. 提交和上传

只添加明确确认过的文件，避免 `git add .`：

```powershell
git add README.md pyproject.toml config.example.json
git add .github/workflows/tests.yml .gitignore .gitattributes
git add src tests
git add "启动助手.bat"
git add docs/GITHUB_UPLOAD_CHECKLIST.md docs/GITHUB_RELEASE_WORKFLOW.md
```

提交：

```powershell
git commit -m "chore: prepare GitHub release"
```

如果还没有远程仓库，在 GitHub 创建空仓库后设置 origin：

```powershell
git remote add origin https://github.com/<owner>/<repo>.git
git branch -M main
git push -u origin main
```

如果已有 origin：

```powershell
git push
```

## 10. 上传后验证

上传后在 GitHub 页面检查：

- README 首页是否正常显示中文。
- `启动助手.bat` 文件名是否正常。
- GitHub Actions 是否通过。
- 仓库文件列表里没有 `data/`、`.venv/`、`diagnostics/`、`.pytest_cache/`。
- 搜索仓库关键词：`cookie`、`token`、`authorization`、`password`、`账号`、`密码`。
- 下载一个全新 zip，按 README 走一遍启动流程。

如果上传后发现隐私泄漏：

1. 立即把 GitHub 仓库设为 private。
2. 删除泄漏文件并提交修复。
3. 如果泄漏的是 Cookie、Token 或浏览器 profile，视为已失效，重新登录并清理旧状态。
4. 如果泄漏已经进入 Git 历史，需要重写历史或重新建仓库；不要只在最新提交删除。

## 11. 最终发布检查清单

- [ ] README 可以让新用户独立完成安装和启动。
- [ ] README 写清楚本地运行、登录流程、自动预约开关和隐私边界。
- [ ] `.gitignore` 覆盖本地数据、缓存、虚拟环境、诊断输出。
- [ ] `config.example.json` 只包含示例值。
- [ ] `启动助手.bat` 默认启用新版 HTTP 预约模式。
- [ ] 本地测试通过。
- [ ] GitHub Actions 配置存在。
- [ ] `git status --ignored --short` 确认隐私目录被忽略。
- [ ] `git ls-files` 中没有数据库、profile、截图、缓存或本地配置。
- [ ] `docs/` 中没有真实账号、Cookie、Token、预约截图或抓包敏感信息。
- [ ] 上传后 GitHub 页面和 CI 均已复查。
