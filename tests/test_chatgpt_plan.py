from core.chatgpt_plan import parse_codex_usage


def test_parse_codex_usage_extracts_weekly_window():
    result = parse_codex_usage({
        "plan_type": "plus",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": 12,
                "limit_window_seconds": 18000,
                "reset_after_seconds": 1200,
                "reset_at": 1800000000,
            },
            "secondary_window": {
                "used_percent": 67,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 500000,
                "reset_at": 1800500000,
            },
        },
    })

    assert result["ok"] is True
    assert result["plan_type"] == "plus"
    assert result["short_used_percent"] == 12
    assert result["weekly_used_percent"] == 67
    assert result["weekly_limit_window_seconds"] == 604800
    assert result["weekly_reset_at"] == 1800500000


def test_parse_codex_usage_treats_free_primary_window_as_weekly():
    result = parse_codex_usage({
        "plan_type": "free",
        "rate_limit": {
            "primary_window": {
                "used_percent": 25,
                "limit_window_seconds": 18000,
                "reset_at": 1800000000,
            },
        },
    })

    assert result["weekly_used_percent"] == 25
    assert "short_used_percent" not in result


def test_parse_codex_usage_extracts_monthly_window_from_additional_limits():
    result = parse_codex_usage({
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {
                "used_percent": 70,
                "limit_window_seconds": 604800,
                "reset_at": 1800500000,
            },
        },
        "additional_rate_limits": [{
            "limit_name": "monthly",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 30,
                    "limit_window_seconds": 2592000,
                    "reset_after_seconds": 1200000,
                    "reset_at": 1801500000,
                },
            },
        }],
    })

    assert result["plan_type"] == "pro"
    assert result["weekly_used_percent"] == 70
    assert result["monthly_used_percent"] == 30
    assert result["monthly_limit_window_seconds"] == 2592000
    assert result["monthly_reset_at"] == 1801500000
