from newsroom import investigation


def test_usable_angles_keeps_only_evidenced_claims():
    data = {
        "angles": [
            {"claim": "Promised in 2024, never built.", "kind": "record", "evidence": [{"url": "https://a.example/x", "source": "Budget speech", "fact": "line 12"}, {"url": "", "source": "hearsay", "fact": ""}]},
            {"claim": "Nobody checked the number.", "kind": "numbers", "evidence": []},
            {"claim": "", "kind": "missing", "evidence": [{"url": "https://a.example/y", "source": "s", "fact": "f"}]},
        ]
    }
    kept = investigation.usable_angles(data)
    assert len(kept) == 1
    assert kept[0]["claim"].startswith("Promised") and len(kept[0]["evidence"]) == 1
    assert investigation.usable_angles(None) == [] and investigation.usable_angles({}) == []
    assert investigation.empty("why") == {"angles": [], "unanswered": [], "summary": "why"}
