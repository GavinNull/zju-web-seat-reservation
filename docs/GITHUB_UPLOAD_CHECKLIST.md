# GitHub 上传检查清单

完整发布流程见 [GitHub 发布 Workflow](GITHUB_RELEASE_WORKFLOW.md)。本文件只保留上传前的短清单。

## 上传前确认

- 不上传 `data/`：包含任务数据库、浏览器登录状态等本地数据。
- 不上传 `output/`：包含测试截图、诊断截图和运行记录，可能带有页面信息。
- 不上传 `diagnostics/`：包含异常截图和诊断文本，可能带有个人页面信息。
- 不上传 `.venv/`、`__pycache__/`、`.pytest_cache/`、`*.egg-info/` 等生成文件。
- 不上传邮箱授权码、统一身份认证密码、Cookie、浏览器 profile。
- 如需公开仓库，先检查 `docs/`、调查脚本和截图材料中是否包含个人信息。

## 推荐上传文件

- `.github/workflows/tests.yml`
- `.gitattributes`
- `.gitignore`
- `README.md`
- `pyproject.toml`
- `config.example.json`
- `启动助手.bat`
- `src/`
- `tests/`
- 经过隐私检查的 `docs/`

## 本地验证命令

```powershell
cd <项目目录>
.\.venv\Scripts\python.exe -m pytest -q
```

不要长期写死旧测试数字；以上传前实际输出为准。

## 上传后确认

- GitHub Actions 通过。
- README 中文显示正常。
- 仓库文件列表没有 `data/`、`.venv/`、`diagnostics/`、截图或本地缓存。
- 在 GitHub 搜索 `cookie`、`token`、`authorization`、`password`、`账号`、`密码`，确认没有真实敏感值。
