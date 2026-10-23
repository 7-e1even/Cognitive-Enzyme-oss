"""
Quality Gate E2E Test — 测试 harvest 质量门禁 4 条路径
先启动服务 (python -m app), 然后运行: python tests/test_quality_gate.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

API = "http://localhost:8420"

# ── Test cases ──────────────────────────────────────
CASES = [
    {
        "name": "✅ 高质量 insight（预期 PASS）",
        "data": {
            "insight": "Spring Boot Actuator 在 2.x 默认暴露 /actuator/env 和 /actuator/configprops 端点，攻击者可通过这些端点获取数据库密码、API Key 等敏感配置。生产环境应配置 management.endpoints.web.exposure.include=health,info 限制暴露范围",
            "context": "审计 Spring Boot 应用的安全配置，发现 Actuator 端点未做访问控制",
            "domain": "java_security",
            "tags": ["spring-boot", "actuator", "信息泄露"],
        },
    },
    {
        "name": "⚠️ 模糊 insight（预期 QUARANTINE 或 REFINE）",
        "data": {
            "insight": "注意检查安全问题",
            "context": "代码审计",
            "domain": "",
            "tags": [],
        },
    },
    {
        "name": "❌ 垃圾 insight（预期 REJECT）",
        "data": {
            "insight": "啊",
            "context": "",
            "domain": "",
            "tags": [],
        },
    },
    {
        "name": "🔁 重复 insight（预期分数降低）",
        "data": {
            "insight": "Spring Boot Actuator 暴露 env 端点导致敏感信息泄露",
            "context": "审计 Spring Boot 安全配置",
            "domain": "java_security",
            "tags": ["spring-boot"],
        },
    },
]


async def main():
    import aiohttp

    print("=" * 60)
    print("Quality Gate E2E Test")
    print("=" * 60)

    async with aiohttp.ClientSession() as session:
        # Health check
        try:
            async with session.get(f"{API}/health") as r:
                if r.status != 200:
                    print(f"❌ 服务未启动: {API}/health 返回 {r.status}")
                    return
                print(f"✓ 服务已启动\n")
        except aiohttp.ClientError:
            print(f"❌ 无法连接 {API}，请先启动服务")
            return

        for i, case in enumerate(CASES, 1):
            print(f"\n{'─' * 50}")
            print(f"[{i}/{len(CASES)}] {case['name']}")
            print(f"  insight: {case['data']['insight'][:60]}...")

            async with session.post(
                f"{API}/api/harvest",
                json=case["data"],
                headers={"Content-Type": "application/json"},
            ) as r:
                result = await r.json()
                print(f"  status:   {r.status}")
                print(f"  response: {json.dumps(result, ensure_ascii=False, indent=2)}")

        # Check quarantine list
        print(f"\n{'─' * 50}")
        print("[Quarantine List]")
        async with session.get(f"{API}/api/enzymes", params={"status": "quarantine"}) as r:
            data = await r.json()
            print(f"  隔离酶数量: {data['total']}")
            for e in data["enzymes"]:
                print(f"  - {e['id'][:8]} | score={e['quality_score']} | {e['then'][:40]}")

    print(f"\n{'=' * 60}")
    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
