"""
从 YAML 文件加载配置，带合理的默认值。
"""

import os
from pathlib import Path
from typing import Any


try:
    import yaml
except ImportError:
    yaml = None


# 默认配置（与用户的 config.yaml 合并）
DEFAULTS: dict[str, Any] = {
    "data": {
        "primary_source": "baostock",
        "timeout": 15,
        "retry_count": 3,
        "retry_backoff_base": 2.0,
        "rate_limit": 0.3,
        "store_raw_prices": False,
        "retention_days": 0,
    },
    "database": {
        "path": "db/stock_data.db",
        "journal_mode": "wal",
    },
    "pipeline": {
        "download": {
            "workers": 10,
            "chunk_size": 200,
            "exclude_today_intraday": True,
        },
        "update": {
            "workers": 15,
            "lookback_days": 5,
        },
    },
    "strategies": {
        "volume_surge": {
            "enabled": True,
            "check_days": 2,
            "avg_days": 22,
            "multiplier_min": 2.1,
            "multiplier_max": 0,
            "pass_ratio": 0.85,
        },
        "ma_breakout": {
            "enabled": True,
            "ma_short": 5,
            "ma_long": 20,
            "vol_multiplier": 1.5,
        },
        "ma_crossover": {
            "enabled": False,
            "fast_ma": 5,
            "slow_ma": 20,
            "confirmation_days": 1,
            "direction": "golden",
        },
        "divergence": {
            "enabled": False,
            "lookback": 30,
            "volume_shrink_threshold": 0.7,
            "price_peak_window": 5,
        },
        "momentum": {
            "enabled": False,
            "period": 20,
            "roc_threshold": 0.05,
            "require_volume": True,
            "volume_multiplier": 1.2,
        },
    },
    "scan": {
        "boards": {
            "sh_main": False,
            "sz_main": False,
            "chi": True,
            "kcb": False,
        },
        "exclude_st": True,
        "exclude_suspended": True,
        "min_avg_volume": 0,
    },
    "output": {
        "console": {"max_rows": 50},
        "csv": {"enabled": True, "filename": "output/candidates.csv"},
        "excel": {"enabled": False, "filename": "output/candidates.xlsx"},
        "db_cache": {"enabled": True},
        "text_log": {"enabled": True, "directory": "output/"},
    },
    "notification": {
        "enabled": False,
        "type": None,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base，返回新字典。"""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _find_config() -> str | None:
    """从当前目录向上搜索 config.yaml。"""
    search_dir = Path.cwd()
    for _ in range(5):
        candidate = search_dir / "config.yaml"
        if candidate.exists():
            return str(candidate)
        parent = search_dir.parent
        if parent == search_dir:
            break
        search_dir = parent
    return None


# 模块级配置缓存
_config: dict | None = None


def load_config(config_path: str | None = None) -> dict:
    """加载配置，与默认值合并。

    参数:
        config_path: config.yaml 路径。为 None 时自动查找。

    返回:
        合并后的配置字典。
    """
    global _config

    if config_path is None:
        config_path = _find_config()

    user_config: dict = {}
    if config_path and os.path.exists(config_path):
        if yaml is not None:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if loaded:
                    user_config = loaded
        else:
            import warnings
            warnings.warn("PyYAML 未安装，将使用默认配置。安装命令: pip install pyyaml")

    _config = _deep_merge(DEFAULTS, user_config)
    return _config


def get_config() -> dict:
    """获取已缓存的配置。需要先调用 load_config()。"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def cfg(*keys: str) -> Any:
    """便捷方法：按 key 路径获取嵌套配置值。

    示例:
        cfg("strategies", "volume_surge", "check_days") -> 2
    """
    conf = get_config()
    for k in keys:
        conf = conf[k]
    return conf
