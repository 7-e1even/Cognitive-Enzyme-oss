# scripts/

一次性或手动运维脚本（非测试、非应用代码）。均从项目根目录运行，连接信息读取 `.env` 中的 `CE_*` 环境变量。

- `run_sync.py` — 全量同步 PG → Qdrant（重建向量库时使用）：`python scripts/run_sync.py`
- `fix_enzyme_ids.py` — 历史迁移：把旧的短 hex 酶 ID 转成合法 UUID（已完成，留档备查）
