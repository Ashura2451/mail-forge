from mailforge.domain.email import classify_address, normalize_email, validate_email


def test_normalization_and_validation_match_legacy_intent():
    assert normalize_email("  Alice@Example.COM ") == "alice@example.com"
    assert validate_email("alice@example.com").valid
    assert not validate_email("alice@localhost").valid
    assert not validate_email("alice@[127.0.0.1]").valid


def test_classification_keeps_consent_and_dns_out_of_syntax_rules():
    result = classify_address("Support+case@MAILINATOR.COM", {"mailinator.com"})
    assert result["is_role_based"]
    assert result["is_disposable"]
    assert result["has_subaddress"]
    assert "consent" not in result
    assert "dns" not in result
