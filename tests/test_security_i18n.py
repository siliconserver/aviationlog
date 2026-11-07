import json
from pathlib import Path

import security

I18N = Path(__file__).resolve().parents[1] / "app" / "i18n"


def test_password_hash():
    h = security.hash_password("geheim123")
    assert security.verify_password("geheim123", h)
    assert not security.verify_password("falsch", h)


def test_recovery_codes_work_once():
    codes, hashes = security.new_recovery_codes()
    assert len(codes) == 10
    remaining = security.use_recovery_code(hashes, codes[3].upper())
    assert remaining is not None and len(remaining) == 9
    assert security.use_recovery_code(remaining, codes[3]) is None
    assert security.use_recovery_code(remaining, "") is None


def test_all_languages_have_all_keys():
    en = json.loads((I18N / "en.json").read_text(encoding="utf-8"))
    for f in I18N.glob("*.json"):
        other = json.loads(f.read_text(encoding="utf-8"))
        missing = sorted(set(en) - set(other))
        assert not missing, f"{f.name} misses {missing[:10]}"
        for key, value in en.items():
            for placeholder in ("{count}", "{name}", "{code}", "{version}", "{years}"):
                if placeholder in value:
                    assert placeholder in other[key], f"{f.name}: {key} lacks {placeholder}"
