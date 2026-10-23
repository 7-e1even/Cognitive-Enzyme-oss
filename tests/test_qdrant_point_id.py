"""_point_id:合法 UUID 原样透传(生产 uuid4 点不重键),其余稳定哈希为 UUID5。"""
import uuid

from app.storage.qdrant_store import _point_id


def test_uuid_passthrough_unchanged():
    u = str(uuid.uuid4())
    assert _point_id(u) == u  # 字节级不变,老库点不孤儿化


def test_readable_id_hashes_to_uuid5_stably():
    a = _point_id("physics-040")
    assert a == _point_id("physics-040")      # 稳定
    assert a != "physics-040"
    uuid.UUID(a)                               # 合法 UUID


def test_all_digit_id_hashes_not_passthrough():
    v = _point_id("12345")
    assert v != "12345"
    uuid.UUID(v)
