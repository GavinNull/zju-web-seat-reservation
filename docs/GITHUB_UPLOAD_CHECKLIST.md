# GitHub 上传检查清单

## 上传前确认

- 不上传 `data/`：包含任务数据库、浏览器登录状态等本地数据。
- 不上传 `output/`：包含测试截图、诊断截图和运行记录，可能带有页面信息。
- 不上传 `.venv/`、`__pycache__/`、`.pytest_cache/`、`*.egg-info/` 等生成文件。
- 不上传邮箱授权码、统一身份认证密码、Cookie、浏览器 profile。
- 如需公开仓库，先检查 `docs/` 和截图材料中是否包含个人信息。

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
- `tools/`
- `docs/`

## 本地验证命令

```powershell
cd <项目目录>
.\.venv\Scripts\python.exe -m pytest -q
```

当前基准：`66 passed`。
