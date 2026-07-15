# 参与贡献

感谢你帮助改进麦块联机 QQ Bot。

## 提交问题

请先搜索已有 Issue。提交新问题时，请提供复现步骤、期望行为、实际行为和已脱敏的日志。不要公开手机号、QQ 号、密码、Cookie、JWT、Client API Key、数据库或 `.env` 内容。

安全漏洞请按 [SECURITY.md](SECURITY.md) 私下报告。

## 本地开发

推荐使用项目自带的隔离测试容器：

```bash
docker compose --profile test build test
docker compose --profile test run --rm test
```

也可以使用 Python 3.10+：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r bot/requirements-dev.txt
python -m pytest bot/tests
```

## Pull Request

- 一个 PR 聚焦一个问题，说明修改动机和验证方式。
